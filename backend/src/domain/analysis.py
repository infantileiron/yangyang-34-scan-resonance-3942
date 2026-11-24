from collections import defaultdict
from math import sqrt
from statistics import mean
from scipy.stats import t


COLUMNS = (
    ("desk", "扫描台"), ("count", "扫描数"), ("amplitude", "平均幅值（μm）"),
    ("streak", "平均纹影（px）"), ("coef", "纹影相关系数"), ("slope", "共同斜率（px/μm）"),
    ("rmse", "调整后 RMSE（px）"), ("p", "台内 p 值"), ("alerts", "达线测次"), ("priority", "复勘编号"),
)


def validate(row):
    if not row["amplitude"] >= 0 or not row["streak"] >= 0:
        raise ValueError("共振幅度和纹影长度均不能为负")


def moments(points):
    return (sum(x*x for x, _ in points), sum(y*y for _, y in points), sum(x*y for x,y in points))


def correlation(sxx, syy, sxy):
    if not min(sxx, syy) > 0:
        return None
    return max(-1., min(1., sxy / sqrt(sxx * syy)))


def analyze(rows, parameters):
    cutoff = parameters.get("alert", 9)
    groups = defaultdict(list)
    seen = set()
    for row in rows:
        validate(row)
        key = row["desk"], row["scan"]
        if key in seen:
            raise ValueError(f"{row['desk']} 存在重复测次 {row['scan']}")
        seen.add(key)
        groups[row["desk"]].append(row)
    if len(groups) < 2:
        raise ValueError("至少登记两张扫描台")
    centered = []
    per_desk = []
    participating = 0
    for desk, scans in sorted(groups.items()):
        scans = sorted(scans,key=lambda row: row["scan"])
        avg_x, avg_y = mean(row["amplitude"] for row in scans), mean(row["streak"] for row in scans)
        points = [(row["amplitude"] - avg_x, row["streak"] - avg_y) for row in scans]
        active = len(scans) >= 2
        if active:
            participating += 1
            centered.extend(points)
        per_desk.append((desk, scans, points, avg_x, avg_y, active))
    df = len(centered) - participating - 1
    sxx, syy, sxy = moments(centered)
    r = correlation(sxx, syy, sxy)
    if participating < 2 or df < 1 or r is None:
        raise ValueError("至少需要两个有重复测次的台，台内共振和纹影应有变化")
    p = 0.0 if abs(r) >= 1 else float(2 * t.sf(abs(r) * sqrt(df / (1 - r*r)), df))
    slope = sxy / sxx
    overall_x, overall_y = mean(row["amplitude"] for row in rows), mean(row["streak"] for row in rows)
    pooled = correlation(*moments([(row["amplitude"] - overall_x, row["streak"] - overall_y) for row in rows]))
    data, chart = [], []
    for desk, scans, points, x, y, active in per_desk:
        alert_rows = sorted((row for row in scans if row["streak"] >= cutoff), key=lambda row:(-row["streak"], row["scan"]))
        error = sqrt(mean((point_y - slope*point_x)**2 for point_x, point_y in points)) if active else None
        data.append(dict(desk=desk,count=len(scans),amplitude=round(x, 3),streak=round(y, 3),
                         rmse=round(error, 3) if error is not None else None, alerts=len(alert_rows),
                         priority="，".join(row["scan"] for row in alert_rows[:6]) if active else "仅一笔，补采"))
        chart.append((desk,len(alert_rows)))
    data.extend([dict(desk="忽略扫描台的粗相关", count=len(rows), coef=round(pooled, 5) if pooled is not None else None),
                 dict(desk="同台去均值后", count=len(centered), coef=round(r,5), slope=round(slope, 5), p=round(p,8))])
    reversal = pooled is not None and pooled*r < 0
    return report("同台相关和粗相关方向相反" if reversal else "已拆开扫描台偏移与台内共振关联",
                  f"使用 {participating} 张有重复测次的扫描台估计共同斜率。实测长度达到 {cutoff:g} px 的记录列入工位复勘。",
                  [("扫描台",len(groups),"张"),("有效台内测次",len(centered),"笔"),("台内相关",round(r,4),"")],
                  COLUMNS,data,"各台纹影达线次数",chart,"笔")


def seed():
    sites = [("窗边一号台", 6,12,"CB1"),("避振二号台",16,9,"BZ2"),("矿物三号台",25,6,"SK3"),("固定四号台",34,4,"GD4")]
    return [dict(desk=desk,scan=f"{prefix}-{index+1:02d}",
                 amplitude=round(x + (index-4)*0.72, 2),
                 streak=round(y + (index-4)*0.4 + (index*3%7-3)*0.12,2))
            for desk,x,y,prefix in sites for index in range(10)]


def report(headline, summary, metrics, columns, rows, chart_label, chart_values, unit=""):
    return {
        "headline": headline, "summary": summary,
        "metrics": [{"label": label, "value": value, "unit": suffix} for label, value, suffix in metrics],
        "columns": [{"key": key, "label": label} for key, label in columns],
        "rows": [{key: row.get(key) for key, _ in columns} for row in rows],
        "chart": {"label": chart_label, "unit": unit,
                  "points": [{"label": label, "value": value} for label, value in chart_values]},
    }
