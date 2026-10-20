#!/usr/bin/env python3
"""A股盘面每日跟踪看板 generator（双层 Tab 架构）.

导航结构：
- 顶层 Session Tab：午盘总结 / 收盘总结（同一交易日的两份快照各自独立成页；
  缺失的 Session 渲染引导空态页，写入对应快照后自动出现完整面板）。
- 内容维度 Tab：总览 / 板块与主线 / 涨停梯队 / 个股与异动 / 消息与盯盘。

数据流：
- data/snapshots/YYYY-MM-DD.json 每日快照（session=午盘/收盘），自动化或人工写入；
- data/source/ 存放 TuShare 原始行情（指数日线、涨跌停/炸板逐日计数）；
- 本脚本合并 history 数据集、按 session 组装 datasets（键前缀 `<session>:<name>`）、
  计算情绪周期得分，输出单文件 index.html 与 dashboard_data.json。
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
SOURCE_DIR = ROOT / "data" / "source"
ECHARTS_JS = ROOT / "echarts.min.js"
DASHBOARD_RUNTIME_JS = ROOT / "dashboard_runtime.js"
DASHBOARD_HTML = ROOT / "index.html"
DASHBOARD_DATA = ROOT / "dashboard_data.json"

DASHBOARD_TITLE = "A股盘面每日跟踪看板"
DASHBOARD_SUBTITLE = "午盘/收盘双快照 · 情绪周期 · 主线梯队 · 复盘清单"
TIMEZONE_LABEL = "Asia/Shanghai"
DEFAULT_RANGE = "ALL"

SESSION_MAP = {"午盘": "mid", "收盘": "close"}
SESSION_LABELS = {"mid": "午盘总结", "close": "收盘总结"}
SESSION_ORDER = ["mid", "close"]

CONTENT_TABS = [
    ("overview", "总览"),
    ("sectors", "板块与主线"),
    ("ladder", "涨停梯队"),
    ("stocks", "个股与异动"),
    ("news", "消息与盯盘"),
]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_session_snapshots() -> tuple[str, dict]:
    """返回 (最新交易日, {session_key: snapshot})；同 session 保留最新 captured_at。"""
    paths = sorted(SNAPSHOT_DIR.glob("*.json"))
    if not paths:
        raise SystemExit("data/snapshots 下没有快照文件")
    snaps = [read_json(p) for p in paths]
    latest_date = max(s["snapshot_date"] for s in snaps)
    by_session: dict[str, dict] = {}
    for snap in snaps:
        if snap["snapshot_date"] != latest_date:
            continue
        skey = SESSION_MAP.get(snap.get("session", ""), "mid")
        prev = by_session.get(skey)
        if prev is None or snap.get("captured_at", "") >= prev.get("captured_at", ""):
            by_session[skey] = snap
    return latest_date, by_session


def load_history(snapshots: dict[str, dict]) -> list[dict]:
    """合并指数日线（合成两市成交额）与涨跌停计数，并叠加当日快照指标。"""
    sh = {r["trade_date"]: r for r in read_json(SOURCE_DIR / "sh_index_daily.json")}
    sz = {r["trade_date"]: r for r in read_json(SOURCE_DIR / "sz_index_daily.json")}
    counts = read_json(SOURCE_DIR / "limit_counts.json")["counts"]

    rows: dict[str, dict] = {}
    for td, r in sh.items():
        d = f"{td[:4]}-{td[4:6]}-{td[6:]}"
        amount_yi = (float(r["amount"]) + float(sz[td]["amount"])) / 1e5
        rows[d] = {
            "date": d,
            "session": "close",
            "sh_close": round(float(r["close"]), 2),
            "sh_pct": round(float(r["pct_chg"]), 2),
            "amount_yi": round(amount_yi),
            "captured_at": "2026-10-15T12:30:00+08:00",
        }
    for d, c in counts.items():
        if d in rows:
            rows[d]["limit_up"] = c["U"]
            rows[d]["limit_down"] = c["D"]
            rows[d]["zhaban"] = c["Z"]

    for snap in snapshots.values():
        captured_at = snap.get("captured_at", "")
        for m in snap.get("metrics", []):
            row = dict(m)
            row.setdefault("captured_at", captured_at)
            prev = rows.get(row["date"])
            if prev is None or row["captured_at"] >= prev.get("captured_at", ""):
                rows[row["date"]] = {**prev, **row} if prev else row
    return [rows[key] for key in sorted(rows)]


def compute_sentiment(sent: dict) -> dict:
    """情绪温度打分（0-100）：可用分项加权归一，缺失分项自动跳过（自动化场景宽容）。"""
    lu, ld, zb = sent.get("limit_up"), sent.get("limit_down"), sent.get("zhaban")
    comps = []
    net_limit = None
    if lu is not None and ld is not None and lu + ld:
        net_limit = (lu - ld) / (lu + ld) * 100
        comps.append((0.25, net_limit))
    seal = sent.get("fengban_rate")
    if seal is None and zb is not None and lu is not None and (lu + zb):
        seal = lu / (lu + zb) * 100
    height = min(sent["max_boards"] / 7, 1) * 100 if sent.get("max_boards") else None
    if seal is not None:
        comps.append((0.20, seal))
    if height is not None:
        comps.append((0.20, height))
    up, down = sent.get("up_count"), sent.get("down_count")
    breadth = None
    if up is not None and down is not None and (up + down):
        breadth = up / (up + down) * 100
        comps.append((0.25, breadth))
    volume = None
    delta = sent.get("turnover_delta_yi")
    if delta is not None and sent.get("turnover_yi"):
        base = sent["turnover_yi"] - delta
        vol_delta_pct = delta / base * 100 if base else 0
        volume = max(0.0, min(100.0, 50 + vol_delta_pct * 3))
        comps.append((0.10, volume))
    total_w = sum(w for w, _ in comps) or 1.0
    score = round(sum(v * w for w, v in comps) / total_w, 1)
    if score < 20:
        phase = "冰点 · 退潮末端"
    elif score < 40:
        phase = "回暖 · 试错期"
    elif score < 60:
        phase = "分歧 · 震荡期"
    elif score < 80:
        phase = "发酵 · 主升期"
    else:
        phase = "高潮 · 过热预警"
    return {
        "score": score,
        "phase": phase,
        "breadth": round(breadth, 1) if breadth is not None else None,
        "net_limit": round(net_limit, 1) if net_limit is not None else None,
        "seal": round(seal, 1) if seal is not None else None,
        "height": round(height, 1) if height is not None else None,
        "volume": round(volume, 1) if volume is not None else None,
    }


def star_label(stars: int) -> str:
    return "★" * stars + "☆" * (5 - stars)


def dated(date: str, items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        row = dict(item)
        row["date"] = date
        out.append(row)
    return out


def ladder_rows(date: str, ladder: list[dict], category: str) -> list[dict]:
    rows = []
    for item in ladder:
        if item["category"] != category:
            continue
        row = dict(item)
        row["date"] = date
        row["seal_label"] = f"{row['seal_yi']:.2f}" if row.get("seal_yi") is not None else "—"
        row["amt_label"] = f"{row['amount_yi']:.1f}" if row.get("amount_yi") is not None else "—"
        row["rate_label"] = f"{row['turnover_pct']:.1f}%" if row.get("turnover_pct") is not None else "—"
        rows.append(row)
    return rows


def ladder_distribution(date: str, ladder: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for item in ladder:
        if item["category"] == "首板":
            label = "首板"
        elif item["category"] == "炸板":
            label = "炸板"
        elif "老妖" in item["boards"] or "中军" in item["boards"]:
            label = "中军/老妖"
        elif "5连板" in item["boards"]:
            label = "5板"
        elif "3板" in item["boards"]:
            label = "3板"
        else:
            label = "2板"
        counts[label] = counts.get(label, 0) + 1
    order = ["首板", "2板", "3板", "5板", "中军/老妖", "炸板"]
    return [
        {"date": date, "label": label, "count": counts.get(label, 0)}
        for label in order
        if counts.get(label, 0) > 0
    ]


def sector_rows(date: str, sectors: dict) -> tuple[list[dict], list[dict]]:
    all_rows = []
    for grp, dir_key, dirname in (
        ("行业", "up_industry", "领涨"),
        ("概念", "up_concept", "领涨"),
        ("行业", "down_industry", "领跌"),
        ("概念", "down_concept", "领跌"),
    ):
        for item in sectors.get(dir_key, []):
            all_rows.append({"date": date, "grp": grp, "dir": dirname, "name": item["name"], "pct": item["pct"]})
    concepts = [
        {"date": date, "dir": r["dir"], "name": r["name"], "pct": r["pct"], "pct_label": f"{r['pct']:+.2f}%"}
        for r in all_rows
        if r["grp"] == "概念"
    ]
    concepts.sort(key=lambda r: -r["pct"])
    return all_rows, concepts


def session_kpis(snapshot: dict, sentiment: dict, prev_close: float | None) -> list[dict]:
    indices = {i["name"]: i for i in snapshot["indices"]}
    sent = snapshot["sentiment"]
    zha_rate = None
    if sent.get("zhaban") is not None:
        zha_rate = sent["zhaban"] / (sent["limit_up"] + sent["zhaban"]) * 100
    kpis = [
        {"id": "kpi-sh", "label": "上证指数", "value": f"{indices['上证指数']['close']:,.2f}",
         "delta": f"{indices['上证指数']['pct']:+.2f}%", "delta_class": "down",
         "detail": f"昨收 {prev_close:,.2f}" if prev_close else "宽基锚点"},
        {"id": "kpi-sz", "label": "深证成指", "value": f"{indices['深证成指']['close']:,.2f}",
         "delta": f"{indices['深证成指']['pct']:+.2f}%", "delta_class": "down", "detail": "深市锚点"},
        {"id": "kpi-cyb", "label": "创业板指", "value": f"{indices['创业板指']['close']:,.2f}",
         "delta": f"{indices['创业板指']['pct']:+.2f}%", "delta_class": "down", "detail": "两市最弱宽基"},
        {"id": "kpi-kc", "label": "科创50", "value": f"{indices['科创50']['close']:,.2f}",
         "delta": f"{indices['科创50']['pct']:+.2f}%", "delta_class": "down",
         "detail": f"北证50 {indices['北证50']['pct']:+.2f}% · 沪深300 {indices['沪深300']['close']:,.2f} ({indices['沪深300']['pct']:+.2f}%)"},
        {"id": "kpi-amt", "label": "成交额", "value": f"{sent['turnover_yi']:,} 亿",
         "delta": f"较昨同期 {sent['turnover_delta_yi']:+} 亿" if sent.get("turnover_delta_yi") is not None else "全天口径",
         "delta_class": "",
         "detail": ("放量但普跌 · 存量博弈" if sent["turnover_delta_yi"] > 0 else "较昨同期缩量 · 关注午后承接")
         if sent.get("turnover_delta_yi") is not None else "量能对照见趋势图"},
    ]
    if sent.get("up_count") is not None and sent.get("down_count") is not None:
        kpis.append({"id": "kpi-breadth", "label": "涨跌家数", "value": f"{sent['up_count']} / {sent['down_count']}",
                     "delta": f"上涨占比 {sentiment['breadth']:.1f}%", "delta_class": "down", "detail": "超5000只普跌 · 结构市"})
    else:
        lianban_n = sum(1 for item in snapshot.get("ladder", []) if item["category"] == "连板")
        kpis.append({"id": "kpi-breadth", "label": "涨跌家数", "value": "待接入",
                     "delta": "广度分项跳过", "delta_class": "", "detail": f"连板梯队 {lianban_n} 家参与评分"})
    kpis.append({"id": "kpi-limit", "label": "涨停 / 跌停", "value": f"{sent['limit_up']} / {sent['limit_down']}",
                 "delta": f"封板率 {sent['fengban_rate']:.0f}%" if sent.get("fengban_rate") is not None else "封板率待收盘补全",
                 "delta_class": "",
                 "detail": f"炸板 {sent['zhaban']} 家 · 炸板率 {zha_rate:.0f}%" if zha_rate is not None else "炸板家数午间未披露"})
    kpis.append({"id": "kpi-phase", "label": "情绪周期", "value": sentiment["phase"],
                 "delta": f"{sentiment['score']} / 100", "delta_class": "",
                 "detail": f"最高 {sent['max_boards']} 板（{sent['max_board_stock']}）"})
    return kpis


def make_dashboard_payload(snapshots: dict[str, dict], history: list[dict]) -> dict:
    trade_date = next(iter(snapshots.values()))["snapshot_date"]
    prev_close = next(
        (r["sh_close"] for r in reversed(history) if r["date"] < trade_date and r.get("session") == "close"),
        None,
    )

    sessions: dict[str, dict] = {}
    datasets: dict[str, list] = {}
    for skey in SESSION_ORDER:
        if skey not in snapshots:
            continue
        snap = snapshots[skey]
        sent = compute_sentiment(snap["sentiment"])
        sessions[skey] = {
            "key": skey,
            "label": SESSION_LABELS[skey],
            "sessionLabel": snap.get("session", ""),
            "snapshotDate": snap["snapshot_date"],
            "capturedAt": snap.get("captured_at", ""),
            "cutoff": "11:30" if skey == "mid" else "15:00",
            "sentiment": sent,
            "stats": {
                "lu": snap["sentiment"]["limit_up"],
                "ld": snap["sentiment"]["limit_down"],
                "zb": snap["sentiment"].get("zhaban"),
                "mb": snap["sentiment"]["max_boards"],
                "stock": snap["sentiment"]["max_board_stock"],
            },
            "kpis": session_kpis(snap, sent, prev_close),
        }
        date = snap["snapshot_date"]
        if snap.get("sectors"):
            sectors, concepts = sector_rows(date, snap["sectors"])
            datasets[f"{skey}:sectors"] = sectors
            datasets[f"{skey}:concepts"] = concepts
        if snap.get("ladder"):
            datasets[f"{skey}:ladderDist"] = ladder_distribution(date, snap["ladder"])
            datasets[f"{skey}:lianban"] = ladder_rows(date, snap["ladder"], "连板")
            datasets[f"{skey}:shouban"] = ladder_rows(date, snap["ladder"], "首板")
            datasets[f"{skey}:zhaban"] = ladder_rows(date, snap["ladder"], "炸板")
        if snap.get("mainlines"):
            datasets[f"{skey}:mainlines"] = dated(date, snap["mainlines"])
        if snap.get("themes"):
            datasets[f"{skey}:themes"] = dated(date, [
                {**t, "stars_label": star_label(t["stars"])} for t in snap["themes"]
            ])
        if snap.get("hot_stocks"):
            datasets[f"{skey}:hotStocks"] = dated(date, [
                {**h, "pct_label": f"{h['pct']:+.2f}%"} for h in snap["hot_stocks"]
            ])
        if snap.get("movers"):
            datasets[f"{skey}:moverStocks"] = dated(date, snap["movers"]["stocks"])
            datasets[f"{skey}:moverSectors"] = dated(date, snap["movers"]["sectors"])
        if snap.get("midday_news"):
            datasets[f"{skey}:newsMidday"] = dated(date, snap["midday_news"])
        if snap.get("close_news"):
            datasets[f"{skey}:newsMidday"] = dated(date, snap["close_news"])
        if snap.get("ferment_news"):
            datasets[f"{skey}:newsFerment"] = dated(date, snap["ferment_news"])
        if snap.get("watchpoints"):
            datasets[f"{skey}:watchpoints"] = dated(date, snap["watchpoints"])
        if snap.get("high_risk"):
            datasets[f"{skey}:highRisk"] = dated(date, snap["high_risk"])
        if snap.get("promotions"):
            datasets[f"{skey}:promotions"] = dated(date, [snap["promotions"]])

    latest_captured = max(s.get("captured_at", "") for s in snapshots.values())
    primary = max(sessions.values(), key=lambda s: s["capturedAt"]) if sessions else None
    missing = [k for k in SESSION_ORDER if k not in sessions]

    return {
        "title": DASHBOARD_TITLE,
        "subtitle": DASHBOARD_SUBTITLE,
        "timezone": TIMEZONE_LABEL,
        "tradeDate": trade_date,
        "generatedAt": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "freshness": {
            "latestDataDate": trade_date,
            "latestCapturedAt": latest_captured,
            "primarySession": primary["sessionLabel"] if primary else "",
            "missingSessions": [SESSION_LABELS[k] for k in missing],
        },
        "availableDates": sorted({r["date"] for r in history}),
        "defaultRange": DEFAULT_RANGE,
        "sessionKeys": list(sessions.keys()),
        "sessions": sessions,
        "datasets": {"history": history, **datasets},
    }


# ---------------------------------------------------------------- renderers


def js_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def json_script(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def esc(value) -> str:
    return html.escape(str(value))


def render_kpi_block(block: dict) -> str:
    delta_class = block.get("delta_class", "")
    return f"""
    <section class="kpi-tile" id="{esc(block["id"])}">
      <p>{esc(block["label"])}</p>
      <strong>{esc(block["value"])}</strong>
      <span class="delta {esc(delta_class)}">{esc(block["delta"])}</span>
      <small>{esc(block["detail"])}</small>
    </section>
    """


def render_panel_actions(block: dict) -> str:
    edit = ""
    edit_command = ""
    if len(block.get("allowed_types", [])) > 1:
        options = "\n".join(
            f'<option value="{esc(kind)}"{" selected" if kind == block.get("initial_type") else ""}>{esc(kind)}</option>'
            for kind in block["allowed_types"]
        )
        edit_command = f"""<button onclick="toggleEdit('{esc(block["chart_id"])}')">Edit</button>"""
        edit = f"""
        <div class="edit-panel" id="edit-{esc(block["chart_id"])}">
          <label for="select-{esc(block["chart_id"])}">Type</label>
          <select id="select-{esc(block["chart_id"])}" onchange="setChartType('{esc(block["chart_id"])}', this.value)">
            {options}
          </select>
        </div>
        """
    return f"""
    <div class="chart-actions">
      {edit}
      <div class="toolbox">
        <button class="tool-button" aria-label="Panel actions" onclick="toggleMenu('{esc(block["chart_id"])}')"><span class="dot"></span><span class="dot"></span><span class="dot"></span></button>
        <div class="menu" id="menu-{esc(block["chart_id"])}">
          {edit_command}
          <button onclick="viewSource('{esc(block["source_key"])}')">View Data Source</button>
        </div>
      </div>
    </div>
    """


def infer_panel_span(block: dict) -> int:
    if block.get("span") is not None:
        span = int(block["span"])
        return span if span in (4, 6, 12) else 6
    if block["kind"] == "table":
        columns = block.get("columns", [])
        has_long_text = any(col.get("long_text") for col in columns)
        return 12 if len(columns) >= 6 or has_long_text else 6
    if block["kind"] == "chart":
        chart_type = str(block.get("initial_type") or "")
        dense_chart = chart_type in {"heatmap", "scatter"} or block.get("dense")
        many_categories = int(block.get("category_count") or 0) > 14
        return 12 if dense_chart or many_categories else 6
    if block["kind"] == "note":
        return 4 if block.get("compact") else 6
    return 6


def render_chart_block(block: dict) -> str:
    span = block.get("span") or infer_panel_span(block)
    return f"""
    <section class="dashboard-panel chart-panel" data-span="{span}" id="{esc(block["id"])}">
      <header>
        <div>
          <h2>{esc(block["title"])}</h2>
          <p>{esc(block["subtitle"])}</p>
        </div>
        {render_panel_actions(block)}
      </header>
      <div class="chart" id="{esc(block["chart_id"])}" role="img" aria-label="{esc(block["title"])}"></div>
      <footer>{esc(block["unit"])} | {esc(block["source_context"])}</footer>
    </section>
    """


def render_table_block(block: dict) -> str:
    span = block.get("span") or infer_panel_span(block)
    columns = block["columns"]
    head = "".join(f"<th>{esc(col['label'])}</th>" for col in columns)
    return f"""
    <section class="dashboard-panel table-panel" data-span="{span}" id="{esc(block["id"])}">
      <header>
        <div>
          <h2>{esc(block["title"])}</h2>
          <p>{esc(block["subtitle"])}</p>
        </div>
        <div class="toolbox">
          <button class="tool-button" aria-label="Panel actions" onclick="toggleMenu('{esc(block["source_key"])}')"><span class="dot"></span><span class="dot"></span><span class="dot"></span></button>
          <div class="menu" id="menu-{esc(block["source_key"])}">
            <button onclick="viewSource('{esc(block["source_key"])}')">View Data Source</button>
          </div>
        </div>
      </header>
      <div class="table-scroll">
        <table id="{esc(block["table_id"])}">
          <thead><tr>{head}</tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <footer>{esc(block["source_context"])}</footer>
    </section>
    """


def render_note_block(block: dict) -> str:
    span = block.get("span") or infer_panel_span(block)
    return f"""
    <section class="dashboard-note" data-span="{span}" id="{esc(block["id"])}">
      <strong>{esc(block["title"])}</strong>
      <span>{esc(block["body"])}</span>
    </section>
    """


# ------------------------------------------------------------- session page


def chart_block(skey: str, base: str, title: str, subtitle: str, unit: str, source_context: str,
                allowed_types: list, initial_type: str, tab: str, span: int | None = None,
                category_count: int | None = None) -> dict:
    block = {
        "kind": "chart",
        "id": f"panel-{base}_{skey}",
        "chart_id": f"{base}_{skey}",
        "source_key": f"{base}_{skey}",
        "title": title,
        "subtitle": subtitle,
        "unit": unit,
        "source_context": source_context,
        "allowed_types": allowed_types,
        "initial_type": initial_type,
        "tab": tab,
    }
    if span:
        block["span"] = span
    if category_count:
        block["category_count"] = category_count
    return block


def table_block(skey: str, base: str, dataset: str, title: str, subtitle: str, source_context: str,
                columns: list, tab: str, limit: int, span: int | None = None) -> dict:
    block = {
        "kind": "table",
        "id": f"panel-{base}_{skey}",
        "table_id": f"{base}Table_{skey}",
        "source_key": f"{base}_{skey}",
        "title": title,
        "subtitle": subtitle,
        "source_context": source_context,
        "columns": columns,
        "tab": tab,
        "limit": limit,
    }
    if span:
        block["span"] = span
    return block


def note_block(skey: str, base: str, title: str, body: str, tab: str, span: int | None = None) -> dict:
    block = {
        "kind": "note",
        "id": f"note-{base}_{skey}",
        "title": title,
        "body": body,
        "tab": tab,
    }
    if span:
        block["span"] = span
    return block


BLOCK_DATASET_DEPS = {
    "sectorInd": "sectors", "sectorCon": "sectors", "mainlines": "mainlines",
    "themes": "themes", "concepts": "concepts", "hotStocks": "hotStocks",
    "moverStocks": "moverStocks", "moverSectors": "moverSectors",
    "newsMidday": "newsMidday", "newsFerment": "newsFerment",
    "watchpoints": "watchpoints", "lianban": "lianban", "shouban": "shouban",
    "zhaban": "zhaban", "ladderDist": "ladderDist",
}


def filter_available_blocks(payload: dict, skey: str, blocks: list[dict]) -> list[dict]:
    """快照缺失某板块数据时，自动剔除对应面板（自动化场景宽容）。"""
    out = []
    for block in blocks:
        if block["kind"] in ("chart", "table") and "_" in block.get("source_key", ""):
            base = block["source_key"].rsplit("_", 1)[0]
            dep = BLOCK_DATASET_DEPS.get(base)
            if dep and not payload["datasets"].get(f"{skey}:{dep}"):
                continue
        out.append(block)
    return out


def build_session_blocks(payload: dict, skey: str) -> list[dict]:
    sess = payload["sessions"][skey]
    sent = sess["sentiment"]
    D = lambda name: f"{skey}:{name}"
    sfx = skey
    blocks: list[dict] = []

    blocks.extend({"kind": "kpi", "tab": "overview", **kpi} for kpi in sess["kpis"])

    # ---- 总览
    blocks.append(chart_block(
        sfx, "indexTrend", "指数与量能趋势",
        "上证收盘（左轴）与两市成交额（右轴），最新一日为快照口径",
        "点 / 亿元", "来源：TuShare index_daily（000001.SH + 399106.SZ）",
        ["line", "bar"], "line", "overview", span=12))
    blocks.append(chart_block(
        sfx, "sentimentTrend", "涨跌停与炸板结构",
        "近 7 个交易日涨停/炸板/跌停家数，观察情绪周期拐点",
        "家", "来源：TuShare limit_list_d 逐日统计（不含ST）+ 当日快照",
        ["bar", "line"], "bar", "overview"))
    blocks.append(chart_block(
        sfx, "sentimentGauge", "情绪温度计",
        "五分项加权：净涨停/封板质量/连板高度/广度/量能",
        "0-100 综合分", "评分公式见「View Data Source」；阶段：冰点/回暖/分歧/发酵/高潮",
        ["gauge"], "gauge", "overview"))
    st = sess["stats"]
    parts = f"涨停{st['lu']}家 / 跌停{st['ld']}家，最高 {st['mb']} 板（{st['stock']}）"
    if sent.get("seal") is not None:
        parts += f"；封板率 {sent['seal']:.0f}%"
    if sent.get("breadth") is not None:
        parts += f"；上涨广度 {sent['breadth']:.1f}%"
    blocks.append(note_block(
        sfx, "phase", f"情绪周期定位：{sent['phase']}（{sent['score']}/100）",
        parts + "。分项评分与阶段判定见「View Data Source」；主线叙事与盯盘计划见「板块与主线」「消息与盯盘」页。"
        "情绪五阶段：冰点 → 回暖 → 分歧 → 发酵 → 高潮，连板晋级率与炸板结构是拐点的先行信号。",
        "overview", span=12))
    blocks.append(note_block(
        sfx, "methodology", "看板方法论（四步复盘）",
        "① 量能对照：成交额与放量方向，确认存量/增量博弈；② 情绪温度：涨跌停比、封板率、炸板率、"
        "连板高度、涨跌家数广度，映射冰点/回暖/分歧/发酵/高潮五阶段；③ 主线判定：主线>支线>轮动"
        "按资金强度排序，题材标注新旧与强弱星级；④ 风险清单：高位分歧提示、解禁与交割日、"
        "「只卖不买」标记。每个交易日追加一份快照，趋势图自动延伸。",
        "overview"))
    blocks.append(note_block(
        sfx, "automation", "每日更新方式",
        "将当日复盘写入 data/snapshots/YYYY-MM-DD.json（午盘或收盘口径，含 metrics 指标行），"
        "再运行 python3 dashboard.py 重新生成看板；指数与涨跌停历史由 TuShare 接口（data/source/）补充，"
        "同一日期以更新的 captured_at 覆盖（午盘→收盘自动替换）。",
        "overview"))

    # ---- 板块与主线
    blocks.append(chart_block(
        sfx, "sectorInd", "行业涨跌对比",
        "领涨 vs 领跌行业（红涨绿跌，A股配色惯例）",
        "%", "来源：快照板块涨跌（申万行业口径）",
        ["bar"], "bar", "sectors", span=6, category_count=12))
    blocks.append(chart_block(
        sfx, "sectorCon", "概念板块涨跌",
        "题材概念维度涨跌幅，观察资金聚焦方向",
        "%", "来源：快照板块涨跌（概念口径），与行业维度互为印证",
        ["bar"], "bar", "sectors", span=6, category_count=12))
    blocks.append(table_block(
        sfx, "mainlines", D("mainlines"), "领涨主线划分",
        "按资金强度排序：主线 > 支线 > 轮动",
        "来源：快照主线划分", [
            {"field": "tier", "label": "层级"},
            {"field": "name", "label": "方向"},
            {"field": "logic", "label": "核心逻辑", "long_text": True},
            {"field": "stocks", "label": "代表股"},
        ], "sectors", limit=10))
    blocks.append(table_block(
        sfx, "themes", D("themes"), "热门题材强弱排序",
        "新=首日/新催化，旧=高位/分歧，退潮题材谨慎参与",
        "来源：快照题材强弱", [
            {"field": "rank", "label": "#", "numeric": True},
            {"field": "name", "label": "题材"},
            {"field": "stars_label", "label": "强度"},
            {"field": "status", "label": "新旧"},
            {"field": "stocks", "label": "代表股"},
        ], "sectors", limit=10))
    blocks.append(table_block(
        sfx, "concepts", D("concepts"), "概念涨跌明细",
        "领涨/领跌概念全景（图表为可视化摘要）",
        "来源：快照板块涨跌（概念口径）", [
            {"field": "dir", "label": "方向"},
            {"field": "name", "label": "概念"},
            {"field": "pct_label", "label": "涨跌幅", "numeric": True},
        ], "sectors", limit=14))

    # ---- 涨停梯队
    blocks.append(chart_block(
        sfx, "ladderDist", "涨停梯队结构",
        "首板/连板高度分布与炸板数，判断梯队完整性",
        "家", "来源：快照代表性个股摘录，非全市场口径（全市场涨停33家）",
        ["bar", "pie"], "bar", "ladder"))
    if payload["datasets"].get(D("highRisk")):
        blocks.append(note_block(
            sfx, "highrisk", "高位分歧 / 换手提示",
            "；".join(f"{item['name']}：{item['note']}" for item in payload["datasets"][D("highRisk")]),
            "ladder"))
    if payload["datasets"].get(D("promotions")):
        promo = payload["datasets"][D("promotions")][0]
        blocks.append(note_block(
            sfx, "promotions", f"连板晋级率：{promo['rate']:.1f}%（{promo['yesterday_lianban']} 家中 {len(promo['promoted'])} 家晋级）",
            f"晋级：{'、'.join(promo['promoted'])}；断板：{'、'.join(promo['broken'])}。"
            "晋级率与炸板结构是情绪周期拐点的量化验证。"
            f"口径：{promo['verify']}。",
            "ladder", span=12))
    blocks.append(table_block(
        sfx, "lianban", D("lianban"), "连板股清单",
        "连续涨停个股：封板质量与题材纯度是核心",
        "封单/成交单位：亿元，换手：%；来源：快照连板清单", [
            {"field": "name", "label": "股票"},
            {"field": "boards", "label": "高度"},
            {"field": "theme", "label": "题材"},
            {"field": "reason", "label": "涨停逻辑", "long_text": True},
            {"field": "seal_label", "label": "封单(亿)", "numeric": True},
            {"field": "amt_label", "label": "成交(亿)", "numeric": True},
            {"field": "rate_label", "label": "换手", "numeric": True},
        ], "ladder", limit=12))
    blocks.append(table_block(
        sfx, "shouban", D("shouban"), "首板股清单",
        "当日首次涨停，按题材聚合观察扩散方向",
        "封单/成交单位：亿元，换手：%；来源：快照首板清单", [
            {"field": "name", "label": "股票"},
            {"field": "theme", "label": "题材"},
            {"field": "reason", "label": "涨停逻辑", "long_text": True},
            {"field": "seal_label", "label": "封单(亿)", "numeric": True},
            {"field": "amt_label", "label": "成交(亿)", "numeric": True},
            {"field": "rate_label", "label": "换手", "numeric": True},
        ], "ladder", limit=20))
    blocks.append(table_block(
        sfx, "zhaban", D("zhaban"), "炸板股清单",
        "触及涨停后回落：分歧与承接力度的风向标",
        "成交单位：亿元，换手：%；来源：快照炸板清单", [
            {"field": "name", "label": "股票"},
            {"field": "boards", "label": "高度"},
            {"field": "theme", "label": "题材"},
            {"field": "time", "label": "炸板时间"},
            {"field": "reason", "label": "炸板原因", "long_text": True},
            {"field": "amt_label", "label": "成交(亿)", "numeric": True},
            {"field": "rate_label", "label": "换手", "numeric": True},
        ], "ladder", limit=10))

    # ---- 个股与异动
    blocks.append(table_block(
        sfx, "hotStocks", D("hotStocks"), "人气股 / 权重股表现",
        "市场焦点个股涨跌幅与驱动说明",
        "来源：快照热门个股", [
            {"field": "name", "label": "名称"},
            {"field": "code", "label": "代码"},
            {"field": "pct_label", "label": "涨跌幅", "numeric": True},
            {"field": "theme", "label": "题材"},
            {"field": "note", "label": "说明"},
        ], "stocks", limit=12))
    blocks.append(table_block(
        sfx, "moverStocks", D("moverStocks"), "个股异动追踪",
        "盘中直线拉升/杀跌的焦点股与驱动",
        "来源：快照盘中异动（个股维度）", [
            {"field": "name", "label": "股票"},
            {"field": "code", "label": "代码"},
            {"field": "theme", "label": "题材"},
            {"field": "action", "label": "动向"},
            {"field": "note", "label": "异动说明", "long_text": True},
        ], "stocks", limit=10))
    blocks.append(table_block(
        sfx, "moverSectors", D("moverSectors"), "板块异动追踪",
        "盘中集体异动板块：代表股与驱动逻辑",
        "来源：快照盘中异动（板块维度）", [
            {"field": "name", "label": "板块"},
            {"field": "logic", "label": "异动逻辑", "long_text": True},
            {"field": "stocks", "label": "代表股"},
        ], "stocks", limit=10))

    # ---- 消息与盯盘
    blocks.append(table_block(
        sfx, "newsMidday", D("newsMidday"), f"{sess['sessionLabel']}消息面",
        "影响盘面的关键资讯，按 利空/催化/数据/事件/风险/提示 标注",
        "来源：财联社 / 新华社 / 证券时报 / 东方财富 / 公司公告等（快照摘录）", [
            {"field": "tag", "label": "标签"},
            {"field": "source", "label": "来源"},
            {"field": "title", "label": "标题"},
            {"field": "detail", "label": "要点", "long_text": True},
        ], "news", limit=12))
    blocks.append(table_block(
        sfx, "newsFerment", D("newsFerment"), "财经发酵新闻",
        "各大财经媒体热点/周末潜在催化，跟踪跨日传导",
        "来源：中金所 / 渠道调研 / 瑞银证券等（快照摘录）", [
            {"field": "tag", "label": "标签"},
            {"field": "source", "label": "来源"},
            {"field": "title", "label": "标题"},
            {"field": "detail", "label": "要点", "long_text": True},
        ], "news", limit=10))
    blocks.append(table_block(
        sfx, "watchpoints", D("watchpoints"), f"{'午后' if skey == 'mid' else '次日'}盯盘重点",
        "关键观察与前瞻方向",
        "来源：快照盯盘重点", [
            {"field": "idx", "label": "#", "numeric": True},
            {"field": "cat", "label": "类型"},
            {"field": "text", "label": "要点", "long_text": True},
        ], "news", limit=12))
    return filter_available_blocks(payload, skey, blocks)


def render_session_page(payload: dict, skey: str) -> str:
    blocks = [b for b in build_session_blocks(payload, skey) if b["kind"] != "kpi"]
    kpis = [b for b in build_session_blocks(payload, skey) if b["kind"] == "kpi"]
    kpi_html = "\n".join(render_kpi_block(b) for b in kpis)

    tab_buttons = "\n".join(
        f'<button data-content-tab="{tab_id}"{active}>{esc(label)}</button>'
        for index, (tab_id, label, active) in enumerate(
            (tid, lab, ' class="active"' if i == 0 else "") for i, (tid, lab) in enumerate(CONTENT_TABS)
        )
    )
    pages = []
    for tab_id, _ in CONTENT_TABS:
        tab_blocks = [b for b in blocks if b["tab"] == tab_id]
        panels = []
        for b in tab_blocks:
            if b["kind"] == "chart":
                panels.append(render_chart_block(b))
            elif b["kind"] == "table":
                panels.append(render_table_block(b))
            elif b["kind"] == "note":
                panels.append(render_note_block(b))
        hidden = "" if tab_id == CONTENT_TABS[0][0] else " hidden"
        pages.append(
            f'<div class="tab-page" data-content-page="{tab_id}"{hidden}>\n'
            f'    <section class="panel-grid">{"".join(panels)}</section>\n  </div>'
        )
    return f"""
  <div class="session-page" data-session-page="{skey}">
    <section class="kpi-grid">{kpi_html}</section>
    <nav class="content-tabs" aria-label="内容维度">{tab_buttons}</nav>
    {"".join(pages)}
  </div>"""


def render_empty_session(skey: str) -> str:
    label = SESSION_LABELS[skey]
    notes = [
        note_block(skey, f"empty-{index}", title, body, "overview", span=6)
        for index, (title, body) in enumerate([
            (f"{label}尚未生成",
             "当前最新交易日仅写入午盘快照。收盘数据通常在 18:30-19:30 后可得"
             "（TuShare 收盘口径更新），或当日收盘复盘完成后，把 session=\"收盘\" 的快照写入 "
             "data/snapshots/ 并重新运行 python3 dashboard.py，本页会自动渲染与午盘一致的完整面板。"),
            ("午盘与收盘的分工",
             "午盘快照（截至 11:30）用于盘中应对：盯主线龙头、炸板风险、量能节奏与尾盘异动；"
             "收盘快照（截至 15:00）用于当日复盘：全天量能与涨跌停终值、晋级率验证、板块全天强弱与次日计划。"
             "同一交易日两份快照共存，在顶部 Session 标签间切换，各自独立成页、互不覆盖。"),
        ])
    ]
    panels = "\n".join(render_note_block(b) for b in notes)
    return f"""
  <div class="session-page" data-session-page="{skey}" hidden>
    <section class="panel-grid">{panels}</section>
  </div>"""


def make_table_config(session_keys: list[str]) -> dict:
    config = {}
    for skey in session_keys:
        t = f"_{skey}"
        D = lambda name: f"{skey}:{name}"
        config.update({
            f"mainlinesTable{t}": {
                "dataset": D("mainlines"),
                "columns": [{"field": "tier"}, {"field": "name"}, {"field": "logic"}, {"field": "stocks"}],
                "limit": 10,
            },
            f"themesTable{t}": {
                "dataset": D("themes"), "sortField": "rank", "sortDirection": "asc",
                "columns": [
                    {"field": "rank", "numeric": True}, {"field": "name"},
                    {"field": "stars_label"}, {"field": "status"}, {"field": "stocks"},
                ],
                "limit": 10,
            },
            f"conceptsTable{t}": {
                "dataset": D("concepts"),
                "columns": [{"field": "dir"}, {"field": "name"}, {"field": "pct_label", "numeric": True}],
                "limit": 14,
            },
            f"lianbanTable{t}": {
                "dataset": D("lianban"),
                "columns": [
                    {"field": "name"}, {"field": "boards"}, {"field": "theme"},
                    {"field": "reason"}, {"field": "seal_label", "numeric": True},
                    {"field": "amt_label", "numeric": True}, {"field": "rate_label", "numeric": True},
                ],
                "limit": 12,
            },
            f"shoubanTable{t}": {
                "dataset": D("shouban"),
                "columns": [
                    {"field": "name"}, {"field": "theme"}, {"field": "reason"},
                    {"field": "seal_label", "numeric": True}, {"field": "amt_label", "numeric": True},
                    {"field": "rate_label", "numeric": True},
                ],
                "limit": 20,
            },
            f"zhabanTable{t}": {
                "dataset": D("zhaban"),
                "columns": [
                    {"field": "name"}, {"field": "boards"}, {"field": "theme"}, {"field": "time"},
                    {"field": "reason"}, {"field": "amt_label", "numeric": True},
                    {"field": "rate_label", "numeric": True},
                ],
                "limit": 10,
            },
            f"hotStocksTable{t}": {
                "dataset": D("hotStocks"),
                "columns": [
                    {"field": "name"}, {"field": "code"}, {"field": "pct_label", "numeric": True},
                    {"field": "theme"}, {"field": "note"},
                ],
                "limit": 12,
            },
            f"moverStocksTable{t}": {
                "dataset": D("moverStocks"),
                "columns": [
                    {"field": "name"}, {"field": "code"}, {"field": "theme"},
                    {"field": "action"}, {"field": "note"},
                ],
                "limit": 10,
            },
            f"moverSectorsTable{t}": {
                "dataset": D("moverSectors"),
                "columns": [{"field": "name"}, {"field": "logic"}, {"field": "stocks"}],
                "limit": 10,
            },
            f"newsMiddayTable{t}": {
                "dataset": D("newsMidday"),
                "columns": [{"field": "tag"}, {"field": "source"}, {"field": "title"}, {"field": "detail"}],
                "limit": 12,
            },
            f"newsFermentTable{t}": {
                "dataset": D("newsFerment"),
                "columns": [{"field": "tag"}, {"field": "source"}, {"field": "title"}, {"field": "detail"}],
                "limit": 10,
            },
            f"watchpointsTable{t}": {
                "dataset": D("watchpoints"), "sortField": "idx", "sortDirection": "asc",
                "columns": [{"field": "idx", "numeric": True}, {"field": "cat"}, {"field": "text"}],
                "limit": 12,
            },
        })
    return config


def make_source_snippets(session_keys: list[str]) -> dict:
    base = {
        "indexTrend": "sh = source/sh_index_daily.json, sz = source/sz_index_daily.json\namount_yi = (sh.amount + sz.amount) / 1e5  # 千元 → 亿元\n按日期范围过滤；最新快照行按 session 标注 午盘/收盘",
        "sentimentTrend": "counts = source/limit_counts.json  # limit_list_d 逐日计数（不含ST）\n当日行来自快照 metrics；过滤后输出 涨停/炸板/跌停 三序列",
        "sentimentGauge": "score = 0.25×净涨停效应 + 0.20×封板质量 + 0.20×连板高度 + 0.25×上涨广度 + 0.10×量能\n净涨停 = (涨停-跌停)/(涨停+跌停)×100；高度 = min(最高板/7,1)×100；量能 = 50 + 放量幅度%×3",
        "sectorInd": "sectors = 快照 sectors 展开 (行业/概念, 领涨/领跌, 名称, pct)\n图表取 grp == \"行业\"，按涨跌幅排序后红涨绿跌着色",
        "sectorCon": "sectors = 快照 sectors 展开；图表取 grp == \"概念\"，红涨绿跌着色",
        "ladderDist": "ladder = 快照涨停天梯（连板/首板/炸板）\n按 boards 标签聚合为 首板/2板/3板/5板/中军老妖/炸板 计数",
        "mainlines": "mainlines = 快照 mainlines，按 主线 > 支线 > 轮动 资金强度排序",
        "themes": "themes = 快照 themes，星级由 stars 生成 ★/☆，按 rank 升序",
        "concepts": "concepts = 快照 sectors 概念维度 12 条，按涨跌幅降序，dir 标注领涨/领跌",
        "lianban": "lianban = 快照 ladder 中 category == 连板 的行，封单/成交/换手缺失显示 —",
        "shouban": "shouban = 快照 ladder 中 category == 首板 的行，按题材聚合",
        "zhaban": "zhaban = 快照 ladder 中 category == 炸板 的行，含炸板时间与原因",
        "hotStocks": "hotStocks = 快照 hot_stocks，pct_label 格式化为带符号百分比",
        "moverStocks": "moverStocks = 快照 movers.stocks，盘中个股异动与动向标注",
        "moverSectors": "moverSectors = 快照 movers.sectors，盘中板块异动逻辑与代表股",
        "newsMidday": "newsMidday = 快照 midday_news（午间消息面），tag 标注属性",
        "newsFerment": "newsFerment = 快照 ferment_news（财经发酵新闻），tag 标注属性",
        "watchpoints": "watchpoints = 快照 watchpoints，cat 归类 主线跟踪/只卖不买/观望/低吸观察/风险监控",
    }
    snippets = {}
    for skey in session_keys:
        for base_key, text in base.items():
            snippets[f"{base_key}_{skey}"] = text
    return snippets


ANALYSIS_LOGIC = """分析逻辑
- 导航结构：顶层 Session 标签切换 午盘/收盘 两套总结页（同一交易日两份快照各自独立，datasets 以 session 为前缀）；
  页内内容维度标签切换 总览 / 板块与主线 / 涨停梯队 / 个股与异动 / 消息与盯盘 五个视图。
- data/snapshots/YYYY-MM-DD.json 为每日盘面快照（session=午盘/收盘），同一 session 保留最新 captured_at（午盘→收盘自动替换）。
- data/source/ 存放 TuShare 原始数据：000001.SH 与 399106.SZ 指数日线（amount 千元，加总/1e5 合成两市成交额亿元），
  以及 limit_list_d 逐日涨跌停/炸板计数（不含ST）。
- compute_sentiment() 五分项打分：0.25×净涨停效应 + 0.20×封板质量 + 0.20×连板高度 + 0.25×上涨广度 + 0.10×量能；
  映射冰点/回暖/分歧/发酵/高潮五阶段。晋级率由昨日涨停名单（limit_times≥2）与今日天梯交叉验证。
- 前端 runtime 按所选日期范围过滤 history 与当日明细行；Session/内容标签切换仅改变可见性并触发 resize 重绘。"""


CHART_JS_BODY = """
function cssToken(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function chartTheme() {
  return {
    text: cssToken("--chart-text"),
    muted: cssToken("--chart-muted"),
    line: cssToken("--chart-line"),
    primary: cssToken("--chart-primary"),
    secondary: cssToken("--chart-secondary"),
    tertiary: cssToken("--chart-tertiary"),
    quaternary: cssToken("--chart-quaternary"),
    palette: [1, 2, 3, 4, 5, 6, 7].map(index => cssToken("--chart-" + index))
  };
}
function axisStyle(extra) {
  const theme = chartTheme();
  const base = {
    axisLabel: { color: theme.muted },
    axisLine: { lineStyle: { color: theme.line } },
    axisTick: { lineStyle: { color: theme.line } },
    splitLine: { lineStyle: { color: theme.line } }
  };
  const merged = Object.assign({}, base, extra || {});
  merged.axisLabel = Object.assign({}, base.axisLabel, (extra || {}).axisLabel || {});
  return merged;
}
function chartBase(...colorKeys) {
  const theme = chartTheme();
  return {
    textStyle: { color: theme.text },
    color: colorKeys.map(key => theme[key] || key)
  };
}
const SESSION_MARKERS = { midday: "午盘", close: "收盘" };
function indexTrendOption(type, fr) {
  const rows = fr("history");
  const theme = chartTheme();
  const dates = rows.map(r => r.date.slice(5).replace("-", "/"));
  const markIdx = rows.length - 1;
  const markLabel = markIdx >= 0 ? (SESSION_MARKERS[rows[markIdx].session] || "收盘") : "";
  const closes = rows.map((r, i) => {
    if (i === markIdx) {
      return {
        value: r.sh_close,
        symbolSize: 10,
        label: { show: true, formatter: markLabel, position: "top", fontSize: 11, color: theme.muted }
      };
    }
    return r.sh_close;
  });
  const amounts = rows.map((r, i) => ({ value: r.amount_yi, itemStyle: i === markIdx ? { opacity: 0.4 } : undefined }));
  return {
    ...chartBase("primary", "secondary"),
    legend: { data: ["上证收盘", "两市成交额"], top: 2, textStyle: { color: theme.muted } },
    tooltip: {
      trigger: "axis",
      formatter: params => {
        const head = params && params[0] ? params[0].axisValue : "";
        const lines = params.map(p => {
          const isAmt = p.seriesName === "两市成交额";
          const val = isAmt ? Number(p.value).toLocaleString() : Number(p.value).toFixed(2);
          return p.marker + " " + p.seriesName + ": " + val + (isAmt ? " 亿" : " 点");
        });
        return head + "<br/>" + lines.join("<br/>");
      }
    },
    grid: { left: 56, right: 64, top: 38, bottom: 26 },
    xAxis: axisStyle({ type: "category", data: dates, axisLabel: { hideOverlap: true } }),
    yAxis: [
      axisStyle({ type: "value", scale: true, axisLabel: { formatter: v => v.toFixed(0) } }),
      axisStyle({ type: "value", axisLabel: { formatter: v => (v / 10000).toFixed(1) + "万亿" }, splitLine: { show: false } })
    ],
    series: [
      { name: "两市成交额", type: "bar", yAxisIndex: 1, data: amounts, barMaxWidth: 16, itemStyle: { color: theme.secondary, opacity: 0.32 } },
      { name: "上证收盘", type: type, yAxisIndex: 0, smooth: type === "line", data: closes, areaStyle: type === "line" ? { opacity: 0.08 } : undefined }
    ]
  };
}
function sentimentTrendOption(type, fr) {
  const rows = fr("history").filter(r => r.limit_up != null);
  const theme = chartTheme();
  const spec = [
    { name: "涨停", key: "limit_up", color: cssToken("--up") },
    { name: "炸板", key: "zhaban", color: theme.tertiary },
    { name: "跌停", key: "limit_down", color: cssToken("--down") }
  ];
  const dates = rows.map(r => r.date.slice(5).replace("-", "/"));
  return {
    ...chartBase(),
    legend: { data: spec.map(s => s.name), top: 2, textStyle: { color: theme.muted } },
    tooltip: { trigger: "axis" },
    grid: { left: 38, right: 16, top: 38, bottom: 26 },
    xAxis: axisStyle({ type: "category", data: dates, axisLabel: { hideOverlap: true } }),
    yAxis: axisStyle({ type: "value" }),
    series: spec.map(s => ({
      name: s.name,
      type: type,
      smooth: type === "line",
      data: rows.map(r => r[s.key]),
      itemStyle: { color: s.color },
      barMaxWidth: 14
    }))
  };
}
function gaugeOption(s) {
  const theme = chartTheme();
  const sess = (dashboardPayload.sessions || {})[s] || {};
  const st = sess.sentiment || { score: 0, phase: "--" };
  return {
    ...chartBase(),
    series: [{
      type: "gauge",
      min: 0,
      max: 100,
      startAngle: 205,
      endAngle: -25,
      radius: "94%",
      axisLine: {
        lineStyle: {
          width: 16,
          color: [
            [0.2, cssToken("--down")],
            [0.4, theme.palette[2]],
            [0.6, theme.palette[6]],
            [0.8, theme.palette[3]],
            [1, cssToken("--up")]
          ]
        }
      },
      pointer: { length: "58%", width: 5, itemStyle: { color: "auto" } },
      axisTick: { show: false },
      splitLine: { show: false },
      axisLabel: { distance: 24, color: theme.muted, fontSize: 10 },
      title: { offsetCenter: [0, "66%"], color: theme.muted, fontSize: 13 },
      detail: { offsetCenter: [0, "28%"], fontSize: 28, color: theme.text, formatter: v => v.toFixed(0) },
      data: [{ value: st.score, name: st.phase }]
    }]
  };
}
function sectorOption(type, fr, grp, s) {
  const rows = fr(s + ":sectors").filter(r => r.grp === grp).slice().sort((a, b) => a.pct - b.pct);
  const theme = chartTheme();
  const up = cssToken("--up");
  const down = cssToken("--down");
  return {
    ...chartBase(),
    tooltip: { trigger: "axis", valueFormatter: v => Number(v).toFixed(2) + "%" },
    grid: { left: 74, right: 48, top: 10, bottom: 24 },
    xAxis: axisStyle({ type: "value", axisLabel: { formatter: v => v + "%" } }),
    yAxis: axisStyle({ type: "category", data: rows.map(r => r.name) }),
    series: [{
      type: "bar",
      data: rows.map(r => ({ value: r.pct, itemStyle: { color: r.pct >= 0 ? up : down } })),
      barMaxWidth: 16,
      label: {
        show: true,
        position: "right",
        formatter: p => (p.value >= 0 ? "+" : "") + p.value.toFixed(2) + "%",
        color: theme.muted,
        fontSize: 11
      }
    }]
  };
}
function ladderDistOption(type, fr, s) {
  const rows = fr(s + ":ladderDist");
  const theme = chartTheme();
  const colorOf = label => {
    if (label === "5板") return cssToken("--up");
    if (label === "炸板") return theme.tertiary;
    if (label === "中军/老妖") return theme.palette[6];
    if (label === "3板") return theme.palette[5];
    if (label === "2板") return theme.secondary;
    return theme.primary;
  };
  if (type === "pie") {
    return {
      ...chartBase(),
      tooltip: { trigger: "item" },
      series: [{
        type: "pie",
        radius: ["40%", "68%"],
        data: rows.map(r => ({ name: r.label, value: r.count, itemStyle: { color: colorOf(r.label) } }))
      }]
    };
  }
  return {
    ...chartBase(),
    tooltip: { trigger: "axis" },
    grid: { left: 38, right: 16, top: 14, bottom: 26 },
    xAxis: axisStyle({ type: "category", data: rows.map(r => r.label) }),
    yAxis: axisStyle({ type: "value" }),
    series: [{
      type: "bar",
      data: rows.map(r => ({ value: r.count, itemStyle: { color: colorOf(r.label) } })),
      barMaxWidth: 26,
      label: { show: true, position: "top", color: theme.muted, fontSize: 11 }
    }]
  };
}
function makeSessionFactories(s) {
  return {
    ["indexTrend_" + s]: (type, fr) => indexTrendOption(type, fr),
    ["sentimentTrend_" + s]: (type, fr) => sentimentTrendOption(type, fr),
    ["sentimentGauge_" + s]: (type, fr) => gaugeOption(s),
    ["sectorInd_" + s]: (type, fr) => sectorOption(type, fr, "行业", s),
    ["sectorCon_" + s]: (type, fr) => sectorOption(type, fr, "概念", s),
    ["ladderDist_" + s]: (type, fr) => ladderDistOption(type, fr, s)
  };
}
const chartFactories = Object.assign({}, ...__SESSION_KEYS__.map(makeSessionFactories));
function activateSessionTab(key) {
  document.querySelectorAll("[data-session-tab]").forEach(b => b.classList.toggle("active", b.dataset.sessionTab === key));
  document.querySelectorAll(".session-page").forEach(p => { p.hidden = p.dataset.sessionPage !== key; });
  window.dispatchEvent(new Event("resize"));
  window.scrollTo({ top: 0, behavior: "auto" });
}
function setupPageTabs() {
  document.querySelectorAll("[data-session-tab]").forEach(btn => {
    btn.addEventListener("click", () => activateSessionTab(btn.dataset.sessionTab));
  });
  document.querySelectorAll(".content-tabs").forEach(bar => {
    bar.querySelectorAll("[data-content-tab]").forEach(btn => {
      btn.addEventListener("click", () => {
        const page = bar.closest(".session-page");
        bar.querySelectorAll("[data-content-tab]").forEach(b => b.classList.toggle("active", b === btn));
        page.querySelectorAll(".tab-page").forEach(tp => { tp.hidden = tp.dataset.contentPage !== btn.dataset.contentTab; });
        window.dispatchEvent(new Event("resize"));
      });
    });
  });
}
const sourceMap = __SOURCE_MAP__;
setupDashboardRuntime({
  datasets: dashboardPayload.datasets,
  availableDates: dashboardPayload.availableDates,
  defaultRange: dashboardPayload.defaultRange,
  initialCharts: __INITIAL_CHARTS__,
  chartFactories,
  sourceMap,
  tables: __TABLES__,
  fullScript: __FULL_SCRIPT__,
  modalSubtitlePrefix: __MODAL_PREFIX__
});
setupPageTabs();
"""


def build_initial_charts(payload: dict, session_keys: list[str]) -> list[dict]:
    charts = []
    for skey in session_keys:
        for block in build_session_blocks(payload, skey):
            if block["kind"] == "chart":
                charts.append({"id": block["chart_id"], "type": block["initial_type"]})
    return charts


CSS = """
:root {
  color-scheme: light;
  --ink: #2f3437;
  --muted: #68707a;
  --faint: #8b95a3;
  --line: #e1e5ea;
  --line-strong: #cbd3dc;
  --panel: #FAFAFA;
  --page: #ffffff;
  --surface: #FAFAFA;
  --soft: #f2f3f5;
  --soft-blue: #f3f6fb;
  --control-bg: rgba(255, 255, 255, 0.94);
  --topbar-bg: rgba(255, 255, 255, 0.96);
  --menu-bg: #ffffff;
  --modal-bg: #ffffff;
  --modal-backdrop: rgba(55, 53, 47, 0.34);
  --table-head: #FAFAFA;
  --table-hover: #f1f2ff;
  --chart-bg: #FAFAFA;
  --chart-text: #2f3437;
  --chart-muted: #68707a;
  --chart-line: #e1e5ea;
  --chart-primary: #2F6BFF;
  --chart-secondary: #00BFA6;
  --chart-tertiary: #FF7A3D;
  --chart-quaternary: #F45BB3;
  --chart-1: #F45BB3;
  --chart-2: #2F6BFF;
  --chart-3: #00BFA6;
  --chart-4: #FF7A3D;
  --chart-5: #9BD82E;
  --chart-6: #7C3AED;
  --chart-7: #FFD23F;
  --brand: #6979F8;
  --brand-hover: #9EA9FF;
  --brand-end: #CDD2FD;
  --brand-text: #ffffff;
  --accent: #2F6BFF;
  --accent-2: #00BFA6;
  --up: #D03A2B;
  --down: #0E9668;
  --warn: #b7791f;
}
html[data-theme="trae-dark"] {
  color-scheme: dark;
  --ink: #f5f9fe;
  --muted: #9599a6;
  --faint: #666b75;
  --line: #2a2d31;
  --line-strong: #3a3f45;
  --panel: #1a1b1d;
  --page: #0c0c0d;
  --surface: #222427;
  --soft: #2a2d31;
  --soft-blue: #202123;
  --control-bg: #202123;
  --topbar-bg: rgba(12, 12, 13, 0.92);
  --menu-bg: #202123;
  --modal-bg: #1a1b1d;
  --modal-backdrop: rgba(0, 0, 0, 0.58);
  --table-head: #222427;
  --table-hover: #202123;
  --chart-bg: #222427;
  --chart-text: #d1d3db;
  --chart-muted: #9599a6;
  --chart-line: #2a2d31;
  --chart-primary: #28d9ff;
  --chart-secondary: #32f08c;
  --chart-tertiary: #f6c85f;
  --chart-quaternary: #ff6b9a;
  --chart-1: #32f08c;
  --chart-2: #28d9ff;
  --chart-3: #a78bfa;
  --chart-4: #f6c85f;
  --chart-5: #ff6b9a;
  --chart-6: #6ea8ff;
  --chart-7: #d1d3db;
  --brand: #32f08c;
  --brand-hover: #0fdc78;
  --brand-end: #32f08c;
  --brand-text: #0c0c0d;
  --accent: #32f08c;
  --accent-2: #0fdc78;
  --up: #ff7a6b;
  --down: #32d192;
}
* { box-sizing: border-box; }
html { background: var(--page); }
body {
  margin: 0;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  background: var(--page);
  color: var(--ink);
  font-size: 1rem;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
[hidden] { display: none !important; }
.topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  border-bottom: 1px solid var(--line);
  background: var(--topbar-bg);
  backdrop-filter: blur(12px);
}
.topbar-inner {
  max-width: 1320px;
  margin: 0 auto;
  padding: 14px 22px;
  display: grid;
  grid-template-columns: minmax(260px, 1fr) auto;
  gap: 18px;
  align-items: center;
}
h1, h2, p { margin: 0; }
h1 { font-size: 22px; font-weight: 500; letter-spacing: 0; }
.subtitle, .freshness, .range-label, .dashboard-panel p, footer, small {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.45;
  font-weight: 400;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
}
.range-label { display: none; }
.segmented {
  display: inline-flex;
  border: 1px solid var(--line);
  border-radius: 8px;
  overflow: hidden;
  background: var(--control-bg);
}
.segmented button, .menu button, .edit-panel button {
  border: 0;
  background: transparent;
  color: var(--ink);
  font: inherit;
  cursor: pointer;
}
.segmented button {
  min-width: 44px;
  height: 34px;
  padding: 0 10px;
  border-right: 1px solid var(--line);
  font-size: 13px;
  font-weight: 400;
}
.segmented button:last-child { border-right: 0; }
.segmented button.active { background: var(--brand); color: var(--brand-text); font-weight: 500; }
.theme-switch button.active { background: var(--brand); color: var(--brand-text); }
.theme-switch button {
  width: 38px;
  min-width: 38px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
}
.theme-switch svg { width: 16px; height: 16px; stroke-width: 2; }
.date-fields { display: inline-flex; align-items: center; gap: 6px; }
input[type="date"] {
  height: 34px;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 0 8px;
  background: var(--control-bg);
  color: var(--ink);
  font: inherit;
  font-size: 13px;
  font-weight: 400;
}
.dashboard-shell {
  max-width: 1320px;
  margin: 0 auto;
  padding: 14px 22px 44px;
}
.session-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 16px;
}
.session-tabs button {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  padding: 10px 18px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--control-bg);
  color: var(--muted);
  font: inherit;
  cursor: pointer;
}
.session-tabs button strong { font-size: 15px; font-weight: 600; color: var(--ink); }
.session-tabs button small { font-size: 12px; }
.session-tabs button.active {
  border-color: var(--brand);
  background: var(--soft-blue);
  box-shadow: inset 0 0 0 1px var(--brand);
}
.session-tabs button.active strong { color: var(--brand); }
.content-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin: 2px 0 16px;
  border-bottom: 1px solid var(--line);
}
.content-tabs button {
  border: 0;
  background: transparent;
  padding: 9px 14px;
  margin-bottom: -1px;
  border-bottom: 2px solid transparent;
  color: var(--muted);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
}
.content-tabs button:hover { color: var(--ink); }
.content-tabs button.active {
  color: var(--brand);
  border-bottom-color: var(--brand);
  font-weight: 500;
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}
.kpi-tile {
  min-height: 122px;
  padding: 15px;
  display: grid;
  align-content: space-between;
  gap: 6px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 12px;
}
.kpi-tile:first-child {
  background: linear-gradient(135deg, var(--brand) 0%, var(--brand-hover) 58%, var(--brand-end) 100%);
  border-color: var(--brand);
}
.kpi-tile p { color: var(--muted); font-size: 13px; font-weight: 500; }
.kpi-tile strong { font-size: 26px; font-weight: 500; letter-spacing: 0; }
.kpi-tile span { color: var(--ink); font-size: 14px; font-weight: 500; line-height: 1.35; }
.kpi-tile small { font-size: 12.5px; font-weight: 400; }
.kpi-tile .delta.up { color: var(--up); }
.kpi-tile .delta.down { color: var(--down); }
.kpi-tile:first-child p,
.kpi-tile:first-child strong,
.kpi-tile:first-child span,
.kpi-tile:first-child small { color: var(--brand-text); }
.panel-grid {
  display: grid;
  grid-template-columns: repeat(12, minmax(0, 1fr));
  gap: 20px 16px;
}
.dashboard-panel {
  min-height: 360px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: transparent;
  border: 0;
  border-radius: 0;
  padding: 0;
}
[data-span="4"] { grid-column: span 4; }
[data-span="6"] { grid-column: span 6; }
[data-span="12"] { grid-column: 1 / -1; }
.dashboard-note {
  min-height: 150px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: transparent;
}
.dashboard-note strong { font-size: 15px; font-weight: 600; }
.dashboard-note span { color: var(--muted); font-size: 13px; line-height: 1.6; }
.dashboard-panel header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  min-height: 42px;
}
.dashboard-panel h2 { font-size: 17px; font-weight: 500; letter-spacing: 0; }
.chart {
  width: 100%;
  height: 276px;
  min-height: 276px;
  padding: 8px 0 6px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--chart-bg);
}
.chart-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  flex: 0 0 auto;
  position: relative;
  z-index: 12;
}
.toolbox { position: relative; flex: 0 0 auto; }
.tool-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 3px;
  width: 34px;
  height: 30px;
  border: 1px solid var(--line-strong);
  border-radius: 999px;
  background: var(--control-bg);
  color: var(--muted);
  cursor: pointer;
  font-size: 0;
  line-height: 0;
  padding: 0;
  opacity: 0;
  transition: opacity 140ms ease, background-color 140ms ease;
}
.tool-button .dot {
  display: block;
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: currentColor;
}
.dashboard-panel:hover .tool-button,
.dashboard-panel:focus-within .tool-button,
.dashboard-note:hover .tool-button,
.dashboard-note:focus-within .tool-button { opacity: 1; }
.menu {
  display: none;
  position: absolute;
  right: 0;
  top: 34px;
  z-index: 40;
  width: 188px;
  padding: 6px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  background: var(--menu-bg);
  box-shadow: 0 8px 18px rgba(32, 33, 36, 0.12);
}
.menu.open { display: block; }
.edit-panel {
  display: none;
  align-items: center;
  gap: 6px;
  height: 28px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--control-bg);
  color: var(--muted);
  font-size: 13px;
}
.edit-panel.open { display: flex; }
.edit-panel label { padding-left: 8px; white-space: nowrap; }
.edit-panel select {
  height: 26px;
  border: 0;
  border-left: 1px solid var(--line);
  border-radius: 0 5px 5px 0;
  padding: 0 24px 0 7px;
  background: transparent;
  color: var(--ink);
  font: inherit;
  font-size: 13px;
  outline: none;
  cursor: pointer;
}
.menu button {
  display: block;
  width: 100%;
  border: 0;
  background: transparent;
  padding: 8px 10px;
  border-radius: 6px;
  text-align: left;
  cursor: pointer;
  color: var(--ink);
  font: inherit;
  font-size: 13px;
}
.menu button:hover, .menu button:focus-visible { background: var(--soft-blue); outline: none; }
.table-scroll {
  overflow: auto;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--surface);
}
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { padding: 10px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }
th { color: var(--muted); font-weight: 500; background: var(--table-head); }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: var(--table-hover); }
.modal-backdrop {
  position: fixed;
  inset: 0;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: var(--modal-backdrop);
  z-index: 50;
}
.modal-backdrop.open { display: flex; }
.modal {
  width: min(860px, 100%);
  max-height: min(780px, 92vh);
  overflow: auto;
  border-radius: 16px;
  background: var(--modal-bg);
  border: 1px solid var(--line-strong);
  box-shadow: 0 18px 48px rgba(55, 53, 47, 0.18);
}
.modal-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  padding: 18px 20px 14px;
  border-bottom: 1px solid var(--line);
}
.modal-head h3 { margin: 0; font-size: 16px; line-height: 1.4; font-weight: 600; }
.modal-subtitle { margin: 4px 0 0; color: var(--muted); font-size: 14px; line-height: 1.45; }
.modal-body { padding: 18px 20px 20px; }
.source-section + .source-section { margin-top: 16px; }
.source-section h4 { margin: 0 0 8px; color: var(--ink); font-size: 14px; line-height: 1.4; font-weight: 600; }
.code-wrap { position: relative; }
pre {
  margin: 0;
  padding: 14px;
  overflow: auto;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--soft);
  color: var(--ink);
  font-size: 12px;
  line-height: 1.5;
}
.close {
  width: 32px;
  height: 32px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 0;
  border-radius: 999px;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  padding: 0;
}
.close svg { width: 18px; height: 18px; stroke-width: 2.1; }
.close:hover, .close:focus-visible { background: var(--soft); outline: none; }
.copy-button {
  position: absolute;
  right: 8px;
  top: 8px;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--control-bg);
  color: var(--muted);
  cursor: pointer;
}
.copy-button svg { width: 15px; height: 15px; stroke-width: 2; }
.copy-button:hover, .copy-button:focus-visible { background: var(--soft); color: var(--ink); outline: none; }
@media (max-width: 900px) {
  .topbar-inner { grid-template-columns: 1fr; }
  .controls { justify-content: flex-start; }
  .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .dashboard-panel, .dashboard-note, [data-span] { grid-column: 1 / -1; }
}
@media (max-width: 620px) {
  .topbar-inner, .dashboard-shell { padding-left: 14px; padding-right: 14px; }
  .kpi-grid { grid-template-columns: 1fr; }
  .segmented { width: 100%; }
  .segmented button { flex: 1; min-width: 0; }
  .date-fields { width: 100%; }
  input[type="date"] { min-width: 0; width: 100%; }
  .chart { height: 240px; min-height: 240px; }
}
"""


def build_html(payload: dict) -> str:
    echarts = ECHARTS_JS.read_text(encoding="utf-8")
    runtime = DASHBOARD_RUNTIME_JS.read_text(encoding="utf-8")
    session_keys = payload["sessionKeys"]

    session_buttons = []
    pages = []
    for skey in SESSION_ORDER:
        label = SESSION_LABELS[skey]
        if skey in session_keys:
            sess = payload["sessions"][skey]
            captured = sess["capturedAt"][11:16] if len(sess["capturedAt"]) >= 16 else ""
            sub = f"{sess['sessionLabel']} · 截至 {sess['cutoff']} · {captured} 生成"
            session_buttons.append(
                f'<button data-session-tab="{skey}"><strong>{esc(label)}</strong><small>{esc(sub)}</small></button>'
            )
            pages.append(render_session_page(payload, skey))
        else:
            session_buttons.append(
                f'<button data-session-tab="{skey}"><strong>{esc(label)}</strong><small>待更新 · 写入快照后自动生成</small></button>'
            )
            pages.append(render_empty_session(skey))
    active_button_key = session_keys[0] if session_keys else "mid"
    session_buttons_html = "\n".join(session_buttons)
    session_buttons_html = session_buttons_html.replace(
        f'<button data-session-tab="{active_button_key}">',
        f'<button data-session-tab="{active_button_key}" class="active">',
        1,
    )

    chart_js = (
        "const dashboardPayload = " + json_script(payload) + ";\n"
        + CHART_JS_BODY
        .replace("__SESSION_KEYS__", json_script(session_keys))
        .replace("__SOURCE_MAP__", json_script(make_source_snippets(session_keys)))
        .replace("__INITIAL_CHARTS__", json_script(build_initial_charts(payload, session_keys)))
        .replace("__TABLES__", json_script(make_table_config(session_keys)))
        .replace("__FULL_SCRIPT__", js_string(ANALYSIS_LOGIC))
        .replace("__MODAL_PREFIX__", js_string("看板面板数据变换 · "))
    )

    missing = payload["freshness"]["missingSessions"]
    missing_text = "、".join(missing) + "待更新" if missing else "全部快照已生成"
    freshness = (
        f"最新数据：{payload['tradeDate']}（{payload['freshness']['primarySession']}） | {missing_text} | "
        f"生成：{payload['generatedAt']} | 指数与涨跌停历史：TuShare（收盘口径，涨跌停统计不含ST） | 仅供研究，不构成投资建议"
    )

    return f"""<!-- Generated by Trae Work -->
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(payload["title"])}</title>
  <style>{CSS}</style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <div>
        <h1>{esc(payload["title"])}</h1>
        <p class="subtitle">{esc(payload["subtitle"])}</p>
        <p class="freshness" id="dataFreshness">{esc(freshness)}</p>
      </div>
      <div class="controls" aria-label="Dashboard time controls">
        <span class="range-label" id="activeRangeLabel"></span>
        <div class="segmented" aria-label="Time preset">
          <button data-range-preset="7D">7D</button>
          <button data-range-preset="30D">30D</button>
          <button data-range-preset="MTD">MTD</button>
          <button data-range-preset="QTD">QTD</button>
          <button data-range-preset="YTD">YTD</button>
          <button data-range-preset="ALL">All</button>
        </div>
        <div class="date-fields">
          <input id="rangeStart" data-range-input type="date" aria-label="Start date">
          <input id="rangeEnd" data-range-input type="date" aria-label="End date">
        </div>
        <div class="segmented theme-switch" aria-label="Theme">
          <button data-theme-choice="light" type="button" aria-label="Light theme" title="Light">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
              <circle cx="12" cy="12" r="4"></circle>
              <path d="M12 2v2"></path>
              <path d="M12 20v2"></path>
              <path d="m4.93 4.93 1.41 1.41"></path>
              <path d="m17.66 17.66 1.41 1.41"></path>
              <path d="M2 12h2"></path>
              <path d="M20 12h2"></path>
              <path d="m6.34 17.66-1.41 1.41"></path>
              <path d="m19.07 4.93-1.41 1.41"></path>
            </svg>
          </button>
          <button data-theme-choice="trae-dark" type="button" aria-label="Dark theme" title="Dark">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
              <path d="M20.99 13.53A8.5 8.5 0 1 1 10.47 3.01 7 7 0 0 0 20.99 13.53Z"></path>
            </svg>
          </button>
        </div>
      </div>
    </div>
  </header>
  <main class="dashboard-shell">
    <nav class="session-tabs" aria-label="Session">
      {session_buttons_html}
    </nav>
    {"".join(pages)}
  </main>
  <div id="modalBackdrop" class="modal-backdrop" role="dialog" aria-modal="true">
    <section class="modal">
      <div class="modal-head">
        <div>
          <h3 id="modalTitle">Data Source</h3>
          <p class="modal-subtitle" id="modalSubtitle"></p>
        </div>
        <button class="close" aria-label="Close" onclick="closeModal()">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
            <path d="M18 6 6 18"></path>
            <path d="m6 6 12 12"></path>
          </svg>
        </button>
      </div>
      <div class="modal-body">
        <section class="source-section">
          <h4>Panel transform</h4>
          <div class="code-wrap">
            <button class="copy-button" aria-label="Copy panel transform" onclick="copyCode('modalSnippet', this)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
                <rect x="9" y="9" width="11" height="11" rx="2"></rect>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
              </svg>
            </button>
            <pre><code id="modalSnippet"></code></pre>
          </div>
        </section>
        <section class="source-section">
          <h4>Analysis logic</h4>
          <div class="code-wrap">
            <button class="copy-button" aria-label="Copy analysis logic" onclick="copyCode('modalCode', this)">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
                <rect x="9" y="9" width="11" height="11" rx="2"></rect>
                <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
              </svg>
            </button>
            <pre><code id="modalCode"></code></pre>
          </div>
        </section>
      </div>
    </section>
  </div>
  <script>{echarts}</script>
  <script>{runtime}</script>
  <script>{chart_js}</script>
</body>
</html>
"""


def main() -> None:
    _, snapshots = read_session_snapshots()
    history = load_history(snapshots)
    payload = make_dashboard_payload(snapshots, history)
    DASHBOARD_DATA.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    DASHBOARD_HTML.write_text(build_html(payload), encoding="utf-8")
    print(f"Wrote {DASHBOARD_HTML}")
    print(f"Wrote {DASHBOARD_DATA}")


if __name__ == "__main__":
    main()
