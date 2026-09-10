# -*- coding: utf-8 -*-
"""
A股智能选股 - 后端服务

数据源：AKShare（stock_zh_a_daily，Sina 行情源）
提供 /api/analyze 接口，对单只 A 股执行五项主力建仓 + 洗盘结束筛选条件。

运行：
    pip install -r requirements.txt
    python app.py
然后浏览器打开 http://localhost:5000/
"""

import math
import datetime
from typing import List, Dict, Any

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

import akshare as ak

app = Flask(__name__)
CORS(app)  # 允许本地 file:// 打开的 HTML 跨域调用，方便直接打开与微信转发


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def normalize_symbol(code: str) -> str:
    """把用户输入归一化为 AKShare 需要的带交易所前缀代码，如 sz000001 / sh600519 / bj430047。"""
    code = (code or "").strip().lower()
    if not code:
        return ""
    # 已经带前缀
    if code.startswith(("sh", "sz", "bj")):
        return code
    digits = "".join(ch for ch in code if ch.isdigit())
    if not digits:
        return code
    head = digits[0]
    if head == "6" or digits.startswith("688") or digits.startswith("9"):
        return "sh" + digits
    if head in ("0", "3"):
        return "sz" + digits
    if head in ("4", "8"):
        return "bj" + digits
    # 兜底
    return "sz" + digits


def safe_round(x, n=2):
    try:
        if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
            return None
        return round(float(x), n)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 数据获取
# ---------------------------------------------------------------------------
def fetch_daily(symbol: str, days: int = 90) -> List[Dict[str, Any]]:
    """
    使用 AKShare 获取前复权日 K 线数据。
    返回按时间升序排列的 dict 列表，字段：
        date, open, high, low, close, volume(股), amount(元), turnover
    """
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days * 2 + 60)  # 多取一些，保证交易日充足
    df = ak.stock_zh_a_daily(
        symbol=symbol,
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="qfq",
    )
    if df is None or len(df) == 0:
        return []
    df = df.sort_values("date").reset_index(drop=True)
    # 截取最近 days 个交易日
    df = df.tail(days).reset_index(drop=True)
    cols = []
    for _, row in df.iterrows():
        cols.append(
            {
                "date": str(row["date"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "amount": float(row["amount"]),
            }
        )
    return cols


# ---------------------------------------------------------------------------
# 五项条件分析
# ---------------------------------------------------------------------------
def analyze(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    对日 K 线执行五项筛选条件，返回结构化结果。
    bars: 按时间升序，最后一根为今日。
    """
    n = len(bars)
    today = bars[-1]
    prev = bars[-2] if n >= 2 else None

    # 今日核心指标
    latest_price = today["close"]
    prev_close = prev["close"] if prev else today["open"]
    change_pct = (latest_price - prev_close) / prev_close * 100 if prev_close else 0.0
    amount_yi = today["amount"] / 1e8  # 成交额，单位亿元

    # ------- 条件1：股价 5~100 元 -------
    cond1 = 5.0 <= latest_price <= 100.0

    # ------- 条件2：今日涨幅 2%~7% -------
    cond2 = 2.0 <= change_pct <= 7.0

    # ------- 条件3：成交额 > 1 亿元 -------
    cond3 = today["amount"] > 1e8

    # ------- 条件4：最近5个交易日主力底部建仓 -------
    # 5日窗口内至少3天“当日量 > 前10日均量×1.3”，且5日累计涨幅 < 5%
    cond4 = False
    cond4_big_days = 0
    cond4_vol_details = []
    cond4_cum_gain = None
    if n >= 16:  # 需要 5 + 10 的数据
        window = bars[-5:]
        cum_gain = (window[-1]["close"] - window[0]["close"]) / window[0]["close"] * 100
        cond4_cum_gain = cum_gain
        idx_of_window_start = n - 5
        for i, bar in enumerate(window):
            abs_idx = idx_of_window_start + i
            prev10 = bars[abs_idx - 10: abs_idx]  # 该日之前10个交易日
            if len(prev10) == 10:
                avg10 = sum(b["volume"] for b in prev10) / 10.0
                big = bar["volume"] > avg10 * 1.3 if avg10 > 0 else False
                if big:
                    cond4_big_days += 1
                cond4_vol_details.append(
                    {
                        "date": bar["date"],
                        "vol": bar["volume"],
                        "avg10": avg10,
                        "ratio": (bar["volume"] / avg10) if avg10 > 0 else 0,
                        "big": big,
                    }
                )
        cond4 = (cond4_big_days >= 3) and (cum_gain < 5.0)

    # ------- 条件5：最近3个交易日洗盘结束 + 站上5日线 -------
    # 长下影线：下影 >= 2%×收盘 且 下影 > 2×实体
    # 缩量横盘：振幅<3% 且 量持续萎缩
    # 取 3 日内出现 任一形态，且今日收盘 > MA5
    cond5_pattern = False
    cond5_pattern_type = ""
    cond5_above_ma5 = False
    cond5_ma5 = None
    cond5_detail = []
    if n >= 5:
        ma5 = sum(b["close"] for b in bars[-5:]) / 5.0
        cond5_ma5 = ma5
        cond5_above_ma5 = latest_price > ma5

        last3 = bars[-3:]
        # 缩量横盘判断
        amp_ok_all = True
        vol_shrink = True
        for i, bar in enumerate(last3):
            pc = bars[n - 3 + i - 1]["close"] if (n - 3 + i - 1) >= 0 else bar["open"]
            amp = (bar["high"] - bar["low"]) / pc * 100 if pc else 0.0
            amp_ok = amp < 3.0
            if not amp_ok:
                amp_ok_all = False
            cond5_detail.append(
                {
                    "date": bar["date"],
                    "amplitude": round(amp, 2),
                    "amp_ok": amp_ok,
                    "vol": bar["volume"],
                }
            )
        # 成交量持续萎缩：vol[0] > vol[1] > vol[2]
        vols = [b["volume"] for b in last3]
        if len(vols) == 3:
            vol_shrink = vols[0] > vols[1] > vols[2]
        sideways = amp_ok_all and vol_shrink

        # 长下影线判断（3日内任一天）
        long_shadow_day = None
        for bar in last3:
            body = abs(bar["close"] - bar["open"])
            lower_shadow = min(bar["open"], bar["close"]) - bar["low"]
            is_long_shadow = (
                lower_shadow >= 0.02 * bar["close"]
                and (lower_shadow > 2 * body if body > 0 else lower_shadow > 0.01 * bar["close"])
            )
            if is_long_shadow:
                long_shadow_day = bar["date"]
                break

        if long_shadow_day:
            cond5_pattern = True
            cond5_pattern_type = "长下影线（" + long_shadow_day + "）"
        elif sideways:
            cond5_pattern = True
            cond5_pattern_type = "缩量横盘（振幅<3%、量持续萎缩）"

        cond5 = cond5_pattern and cond5_above_ma5
    else:
        cond5 = False

    all_pass = cond1 and cond2 and cond3 and cond4 and cond5

    # 主力资金信号 / 洗盘状态 派生展示
    if cond4:
        main_signal = "主力建仓信号（5日内放量" + str(cond4_big_days) + "天且未大涨）"
    elif cond4_big_days > 0:
        main_signal = "温和放量（" + str(cond4_big_days) + "/5 天放量）"
    else:
        main_signal = "无明显放量"
    if cond5_pattern and cond5_above_ma5:
        wash_status = "洗盘结束，可能即将拉升"
    elif cond5_pattern:
        wash_status = "出现洗盘形态，但未站上5日线"
    elif cond5_above_ma5:
        wash_status = "站上5日线，但未见明显洗盘形态"
    else:
        wash_status = "仍在洗盘/调整中"

    return {
        "latest_price": safe_round(latest_price),
        "prev_close": safe_round(prev_close),
        "change_pct": safe_round(change_pct, 2),
        "amount_yi": safe_round(amount_yi, 2),  # 亿元
        "today": {
            "date": today["date"],
            "open": safe_round(today["open"]),
            "high": safe_round(today["high"]),
            "low": safe_round(today["low"]),
            "close": safe_round(latest_price),
            "volume": int(today["volume"]),
            "amount": safe_round(today["amount"], 0),
        },
        "conditions": {
            "c1_price_range": {
                "pass": cond1,
                "desc": "股价 5~100 元",
                "value": safe_round(latest_price),
            },
            "c2_today_gain": {
                "pass": cond2,
                "desc": "今日涨幅 2%~7%",
                "value": safe_round(change_pct, 2),
            },
            "c3_amount": {
                "pass": cond3,
                "desc": "成交额 > 1 亿元",
                "value": safe_round(amount_yi, 2),
            },
            "c4_main_build": {
                "pass": cond4,
                "desc": "近5日主力建仓（≥3天放量、累计涨幅<5%）",
                "big_days": cond4_big_days,
                "cum_gain": safe_round(cond4_cum_gain, 2) if cond4_cum_gain is not None else None,
                "details": cond4_vol_details[-5:],
            },
            "c5_wash_end": {
                "pass": cond5,
                "desc": "近3日洗盘结束+站上5日线",
                "pattern": cond5_pattern,
                "pattern_type": cond5_pattern_type,
                "above_ma5": cond5_above_ma5,
                "ma5": safe_round(cond5_ma5),
                "details": cond5_detail,
            },
            "all_pass": all_pass,
        },
        "signals": {
            "main_capital": main_signal,
            "wash_status": wash_status,
        },
        "recent": [
            {
                "date": b["date"],
                "open": safe_round(b["open"]),
                "high": safe_round(b["high"]),
                "low": safe_round(b["low"]),
                "close": safe_round(b["close"]),
                "volume": int(b["volume"]),
                "amount_yi": safe_round(b["amount"] / 1e8, 2),
            }
            for b in bars[-10:]
        ],
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_file("index.html")


@app.route("/api/analyze", methods=["GET"])
def api_analyze():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"ok": False, "msg": "请输入股票代码"}), 400
    symbol = normalize_symbol(code)
    try:
        bars = fetch_daily(symbol, days=90)
        if not bars or len(bars) < 16:
            return jsonify(
                {"ok": False, "msg": "获取数据失败或数据不足，请检查股票代码是否正确（如 000001 / 600519）"}
            ), 200
        result = analyze(bars)
        result["ok"] = True
        result["symbol"] = symbol
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "msg": "查询出错：" + str(e)[:200]}), 200


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "akshare": ak.__version__})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
