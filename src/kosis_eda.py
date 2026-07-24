# -*- coding: utf-8 -*-
"""KOSIS 통계표 카탈로그 EDA.

입력:
  data/kosis_catalog_v1.jsonl          (전체 통계표 265,094개, 트리 기반)
  data/kosis_catalog_v2_sample600.jsonl (메타 보강 표본 600개: 차원·항목·단위·주기)
  data/claims_v1.jsonl                 (뉴스 claim 구조화 결과, 대응 확인용)

출력:
  reports/figures/eda_fig01 ~ eda_fig08 (*.png)
  reports/figures/eda_stats.json        (그림에 쓰인 수치 요약)

실행:
  python src/kosis_eda.py
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parents[1]
FIG_DIR = ROOT / "reports" / "figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)

# ── 스타일 ────────────────────────────────────────────────────────────
BLUE = "#2a78d6"      # KOSIS 통계표 측 (categorical slot 1)
GREEN = "#008300"     # 뉴스 claim 측 (categorical slot 2)
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e5e4df"

plt.rcParams.update({
    "font.family": "Malgun Gothic",
    "axes.unicode_minus": False,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": GRID,
    "axes.linewidth": 0.8,
    "axes.grid": True,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "axes.axisbelow": True,
    "text.color": INK,
    "axes.labelcolor": INK2,
    "xtick.color": INK2,
    "ytick.color": INK2,
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
})

comma = FuncFormatter(lambda x, _: f"{int(x):,}")


def style_ax(ax, xgrid=False):
    ax.spines[["top", "right"]].set_visible(False)
    if xgrid:
        ax.grid(axis="x")
        ax.grid(axis="y", visible=False)
    else:
        ax.grid(axis="y")
        ax.grid(axis="x", visible=False)


def save(fig, name):
    fig.tight_layout()
    out = FIG_DIR / name
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("saved", out.relative_to(ROOT))


# ── 데이터 적재 ───────────────────────────────────────────────────────
def iter_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


print("loading catalog v1 ...")
depth_counter = Counter()
top_cat = Counter()
org_counter = Counter()
n_v1 = 0
for r in iter_jsonl(ROOT / "data" / "kosis_catalog_v1.jsonl"):
    n_v1 += 1
    paths = r.get("category_paths") or []
    if paths:
        p = paths[0]
        depth_counter[len(p)] += 1
        top_cat[p[0]] += 1
    org_counter[r.get("org_id")] += 1

print("loading catalog v2 (enriched) ...")
dims_per = Counter()
dim_names = Counter()
units = Counter()
period_types = Counter()
items_per = Counter()
dim_value_counts = []
n_v2 = 0
for r in iter_jsonl(ROOT / "data" / "kosis_catalog_v2_sample600.jsonl"):
    n_v2 += 1
    dims = r.get("dimensions") or []
    dims_per[len(dims)] += 1
    for d in dims:
        dim_names[d["obj_nm"]] += 1
        dim_value_counts.append(d.get("value_count", 0))
    for u in r.get("units") or []:
        units[u] += 1
    for p in r.get("period_types") or []:
        period_types[p] += 1
    items_per[len(r.get("items") or [])] += 1

print("loading claims v1 ...")
change_type = Counter()
claim_units = Counter()
claim_period = Counter()
values_per_claim = Counter()
n_claims = 0
for r in iter_jsonl(ROOT / "data" / "claims_v1.jsonl"):
    c = r["claim"]
    n_claims += 1
    if c.get("change_type"):
        change_type[c["change_type"]] += 1
    if c.get("unit_norm"):
        claim_units[c["unit_norm"]] += 1
    if c.get("period_type"):
        claim_period[c["period_type"]] += 1
    values_per_claim[len(c.get("raw_value_list") or [])] += 1

# ── Fig 1. 분류 계층 깊이 분포 ────────────────────────────────────────
fig, ax = plt.subplots(figsize=(7.5, 4.2))
depths = sorted(depth_counter)
vals = [depth_counter[d] for d in depths]
ax.bar([str(d) for d in depths], vals, color=BLUE, width=0.62)
mode_d = max(depth_counter, key=depth_counter.get)
for i, (d, v) in enumerate(zip(depths, vals)):
    if v >= 25000:
        ax.annotate(f"{v:,}", (i, v), ha="center", va="bottom", fontsize=9, color=INK2)
ax.set_title(f"KOSIS 통계표 분류 계층 깊이 — 표 {n_v1:,}개, 최빈 깊이 {mode_d}단계")
ax.set_xlabel("분류 경로 깊이 (단계)")
ax.set_ylabel("통계표 수")
ax.yaxis.set_major_formatter(comma)
style_ax(ax)
save(fig, "eda_fig01_category_depth.png")

# ── Fig 2. 최상위 주제 분포 ──────────────────────────────────────────
fig, ax = plt.subplots(figsize=(7.5, 5.2))
top15 = top_cat.most_common(15)[::-1]
labels = [k for k, _ in top15]
vals = [v for _, v in top15]
ax.barh(labels, vals, color=BLUE, height=0.62)
ax.annotate(f"{vals[-1]:,} ({vals[-1]/n_v1:.0%})", (vals[-1], len(vals) - 1),
            va="center", ha="right", fontsize=9, color="white",
            xytext=(-6, 0), textcoords="offset points")
ax.set_title("최상위 주제 분포 (상위 15) — 지역통계가 전체의 60%")
ax.set_xlabel("통계표 수")
ax.xaxis.set_major_formatter(comma)
style_ax(ax, xgrid=True)
save(fig, "eda_fig02_top_categories.png")

# ── Fig 3. 작성기관 집중도 (파레토) ──────────────────────────────────
fig, ax = plt.subplots(figsize=(7.5, 4.2))
org_sorted = [v for _, v in org_counter.most_common()]
cum = []
s = 0
for v in org_sorted:
    s += v
    cum.append(s / n_v1 * 100)
ax.plot(range(1, len(cum) + 1), cum, color=BLUE, linewidth=2)
for k in (10, 50):
    ax.annotate(f"상위 {k}개 기관 = {cum[k-1]:.0f}%",
                (k, cum[k - 1]), xytext=(k + 12, cum[k - 1] - 7),
                fontsize=10, color=INK2,
                arrowprops=dict(arrowstyle="-", color=INK2, lw=0.8))
ax.set_title(f"작성기관 누적 점유율 — 기관 {len(org_counter)}곳의 긴 꼬리")
ax.set_xlabel("기관 순위 (표 수 기준)")
ax.set_ylabel("누적 점유율 (%)")
ax.set_ylim(0, 105)
style_ax(ax)
save(fig, "eda_fig03_org_pareto.png")

# ── Fig 4. 표당 차원 수 + 상위 차원명 ────────────────────────────────
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.6), width_ratios=[1, 1.5])
ks = sorted(dims_per)
vals = [dims_per[k] for k in ks]
ax1.bar([str(k) for k in ks], vals, color=BLUE, width=0.6)
for i, v in enumerate(vals):
    ax1.annotate(f"{v}", (i, v), ha="center", va="bottom", fontsize=9, color=INK2)
ax1.set_title(f"표당 차원(분류축) 수 — n={n_v2}")
ax1.set_xlabel("차원 수")
ax1.set_ylabel("통계표 수")
style_ax(ax1)

top_dims = dim_names.most_common(12)[::-1]
ax2.barh([k for k, _ in top_dims], [v for _, v in top_dims], color=BLUE, height=0.62)
ax2.set_title("자주 등장하는 차원명 (상위 12)")
ax2.set_xlabel("등장 표 수")
style_ax(ax2, xgrid=True)
save(fig, "eda_fig04_dimensions.png")

# ── Fig 5. 수록 주기 + 단위 ──────────────────────────────────────────
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.4))
pt = period_types.most_common()
ax1.bar([k for k, _ in pt], [v for _, v in pt], color=BLUE, width=0.6)
ax1.set_title(f"수록 주기 분포 (표본 {n_v2}개)")
ax1.set_ylabel("통계표 수")
style_ax(ax1)

ut = units.most_common(10)[::-1]
ax2.barh([k for k, _ in ut], [v for _, v in ut], color=BLUE, height=0.62)
ax2.set_title("표 단위 분포 (상위 10)")
ax2.set_xlabel("통계표 수")
style_ax(ax2, xgrid=True)
save(fig, "eda_fig05_period_units.png")

# ── Fig 6. 차원 값 개수 분포 (셀 폭발) ───────────────────────────────
fig, ax = plt.subplots(figsize=(7.5, 4.2))
bins = [1, 3, 6, 11, 21, 51, 101, 501, 10000]
bin_labels = ["1-2", "3-5", "6-10", "11-20", "21-50", "51-100", "101-500", "500+"]
bucket = Counter()
for v in dim_value_counts:
    for lo, hi, lab in zip(bins[:-1], bins[1:], bin_labels):
        if lo <= v < hi:
            bucket[lab] += 1
            break
vals = [bucket[l] for l in bin_labels]
ax.bar(bin_labels, vals, color=BLUE, width=0.62)
med = statistics.median(dim_value_counts)
mx = max(dim_value_counts)
ax.set_title(f"차원 하나가 갖는 값 개수 — 중앙값 {med:.0f}개, 최대 {mx:,}개")
ax.set_xlabel("차원 값(카테고리) 개수 구간")
ax.set_ylabel("차원 수")
style_ax(ax)
save(fig, "eda_fig06_dim_value_counts.png")

# ── Fig 7. 뉴스 claim 측: 유형·값 개수 ──────────────────────────────
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.4))
ct = change_type.most_common()
ax1.bar([k for k, _ in ct], [v for _, v in ct], color=GREEN, width=0.6)
for i, (_, v) in enumerate(ct):
    ax1.annotate(f"{v:,}", (i, v), ha="center", va="bottom", fontsize=9, color=INK2)
ax1.set_title(f"뉴스 claim 유형 분포 — n={n_claims:,}")
ax1.set_ylabel("claim 수")
ax1.yaxis.set_major_formatter(comma)
style_ax(ax1)

ks = sorted(values_per_claim)
grouped = Counter()
for k in ks:
    grouped[str(k) if k <= 4 else "5+"] += values_per_claim[k]
order = ["1", "2", "3", "4", "5+"]
multi = sum(v for k, v in values_per_claim.items() if k >= 2)
ax2.bar(order, [grouped[k] for k in order], color=GREEN, width=0.6)
ax2.set_title(f"claim당 수치 개수 — {multi/n_claims:.0%}가 다중 수치")
ax2.set_xlabel("claim 하나에 포함된 수치 개수")
ax2.set_ylabel("claim 수")
ax2.yaxis.set_major_formatter(comma)
style_ax(ax2)
save(fig, "eda_fig07_claims.png")

# ── Fig 8. 단위 어휘 대응: KOSIS vs 뉴스 ─────────────────────────────
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.6))
ut = units.most_common(8)[::-1]
ax1.barh([k for k, _ in ut], [v for _, v in ut], color=BLUE, height=0.62)
ax1.set_title("KOSIS 표 단위 (상위 8)")
ax1.set_xlabel("통계표 수")
style_ax(ax1, xgrid=True)

cu = claim_units.most_common(8)[::-1]
ax2.barh([k for k, _ in cu], [v for _, v in cu], color=GREEN, height=0.62)
ax2.set_title("뉴스 claim 단위 unit_norm (상위 8)")
ax2.set_xlabel("claim 수")
style_ax(ax2, xgrid=True)
save(fig, "eda_fig08_unit_vocab.png")

# ── 수치 요약 저장 ───────────────────────────────────────────────────
stats = {
    "catalog_v1": {
        "n_tables": n_v1,
        "depth_distribution": dict(sorted(depth_counter.items())),
        "top_categories": top_cat.most_common(15),
        "n_orgs": len(org_counter),
        "top10_org_share": round(sum(v for _, v in org_counter.most_common(10)) / n_v1, 4),
    },
    "catalog_v2_enriched": {
        "n_tables": n_v2,
        "dims_per_table": dict(sorted(dims_per.items())),
        "top_dim_names": dim_names.most_common(20),
        "period_types": period_types.most_common(),
        "units_top": units.most_common(15),
        "items_per_table_max": max(items_per),
        "dim_value_count_median": statistics.median(dim_value_counts),
        "dim_value_count_max": max(dim_value_counts),
    },
    "claims_v1": {
        "n_claims": n_claims,
        "change_type": change_type.most_common(),
        "unit_norm_top": claim_units.most_common(15),
        "period_type": claim_period.most_common(),
        "multi_value_share": round(multi / n_claims, 4),
    },
}
with open(FIG_DIR / "eda_stats.json", "w", encoding="utf-8") as f:
    json.dump(stats, f, ensure_ascii=False, indent=2)
print("saved reports/figures/eda_stats.json")
