#!/usr/bin/env python3
"""
heatmap_portal.py - Browser portal for heatmaps of large data.

Run:
    pip install streamlit pandas numpy matplotlib seaborn scipy scikit-learn openpyxl
    streamlit run heatmap_portal.py

    # for files bigger than 200 MB:
    streamlit run heatmap_portal.py --server.maxUploadSize 2000

Workflow in the browser:
    1. Browse / upload a CSV, TSV, TXT or Excel file (or type a local path, or use demo data)
    2. Pick preprocessing, clustering and colour options in the sidebar
    3. Click "Plot" -> see heatmap WITHOUT clustering and WITH clustering
    4. Download plots (PNG / PDF / SVG) and cluster assignments (CSV)
"""

import io
import os

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
import seaborn as sns
import streamlit as st

HIER_METHODS = ["average", "complete", "single", "weighted", "ward", "centroid", "median"]
EUCLID_ONLY = {"ward", "centroid", "median"}
METRICS = ["euclidean", "correlation", "cosine", "cityblock", "chebyshev",
           "braycurtis", "canberra", "sqeuclidean"]
POPULAR_CMAPS = ["RdBu_r", "coolwarm", "bwr", "seismic", "RdYlBu_r", "PiYG", "PRGn",
                 "viridis", "magma", "inferno", "plasma", "cividis", "YlOrRd",
                 "Blues", "Reds", "Greens", "Purples", "Greys", "rocket", "mako",
                 "vlag", "icefire", "Spectral_r"]

st.set_page_config(page_title="Heatmap Portal", layout="wide")


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner="Reading file...")
def read_table(raw: bytes, name: str, sep_choice: str, header: bool, index_col: bool, sheet):
    ext = os.path.splitext(name)[1].lower()
    hdr = 0 if header else None
    idx = 0 if index_col else None
    buf = io.BytesIO(raw)
    if ext in (".xlsx", ".xls", ".xlsm"):
        return pd.read_excel(buf, sheet_name=sheet if sheet else 0, header=hdr, index_col=idx)
    sep = {"auto": None, "comma ,": ",", "tab \\t": "\t", "semicolon ;": ";",
           "space": r"\s+"}[sep_choice]
    if sep is None:
        sep = "\t" if ext in (".tsv", ".txt", ".tab") else ","
        head = raw[:5000].decode("utf-8", "ignore")
        if ext == ".txt" and "\t" not in head:
            sep = "," if "," in head else r"\s+"
    engine = "python" if sep == r"\s+" else "c"
    return pd.read_csv(buf, sep=sep, header=hdr, index_col=idx, engine=engine,
                       low_memory=False) if engine == "c" else \
        pd.read_csv(buf, sep=sep, header=hdr, index_col=idx, engine=engine)


@st.cache_data
def excel_sheets(raw: bytes):
    return pd.ExcelFile(io.BytesIO(raw)).sheet_names


@st.cache_data
def demo_data(n_rows=3000, n_cols=30, seed=0):
    rng = np.random.default_rng(seed)
    rg, cg = rng.integers(0, 5, n_rows), rng.integers(0, 4, n_cols)
    pat = rng.normal(0, 2, (5, 4))
    x = pat[rg][:, cg] + rng.normal(0, 1, (n_rows, n_cols))
    return pd.DataFrame(x, index=[f"feature_{i}" for i in range(n_rows)],
                        columns=[f"sample_{j}" for j in range(n_cols)])


def preprocess(df, o):
    df = df.select_dtypes(include=[np.number]).astype(np.float32)
    if o["nan"] == "drop rows":
        df = df.dropna()
    elif o["nan"] == "fill 0":
        df = df.fillna(0)
    else:
        df = df.apply(lambda r: r.fillna(r.mean()), axis=1)
    df = df.loc[df.notna().any(axis=1)]
    if o["log"]:
        df = np.log2(df.clip(lower=0) + 1)
    df = df.loc[df.std(axis=1) > 0]
    if o["top_n"] and df.shape[0] > o["top_n"]:
        df = df.loc[df.var(axis=1).nlargest(o["top_n"]).index]
    if o["subsample"] and df.shape[0] > o["subsample"]:
        df = df.sample(n=o["subsample"], random_state=42)
    if o["scale"] == "z-score rows":
        df = df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)
    elif o["scale"] == "z-score columns":
        df = (df - df.mean()) / df.std()
    elif o["scale"] == "min-max rows":
        mn, mx = df.min(axis=1), df.max(axis=1)
        df = df.sub(mn, axis=0).div((mx - mn).replace(0, 1), axis=0)
    if o["clip"]:
        df = df.clip(-o["clip"], o["clip"])
    if o["transpose"]:
        df = df.T
    return df.dropna(axis=1, how="all")


# --------------------------------------------------------------------------- #
# Plotting
# --------------------------------------------------------------------------- #
def get_cmap(o):
    if o["custom"]:
        cm = LinearSegmentedColormap.from_list("custom", o["custom_colors"], N=256)
    else:
        cm = plt.get_cmap(o["cmap"])
    return cm.reversed() if o["reverse"] else cm


def limits(df, o):
    v = df.to_numpy()
    if o["manual_limits"]:
        vmin, vmax = o["vmin"], o["vmax"]
    elif o["robust"]:
        vmin, vmax = np.nanpercentile(v, [2, 98])
    else:
        vmin, vmax = np.nanmin(v), np.nanmax(v)
    c = o["center"]
    if c is not None:
        span = max(abs(vmax - c), abs(c - vmin))
        vmin, vmax = c - span, c + span
    return float(vmin), float(vmax)


def show_rows(df, o):
    m = o["row_labels"]
    return m == "Show all" or (m.startswith("Auto") and df.shape[0] <= o["max_labels"])


def show_cols(df, o):
    m = o["col_labels"]
    return m == "Show all" or (m.startswith("Auto") and df.shape[1] <= o["max_labels"])


# largest figure side in inches (~2,200 row names at font 6)
def max_inches(o):
    return 250


def size(df, o):
    w = o["width"] or min(max(6, df.shape[1] * 0.25 + 3), 24)
    h = o["height"] or min(max(6, df.shape[0] * 0.08 + 2), 24)
    if o["fit_labels"]:
        line = o["font"] * 1.35 / 72          # inches per label line
        if show_rows(df, o) and not o["height"]:
            h = max(h, df.shape[0] * line + 3)
        if show_cols(df, o) and not o["width"]:
            w = max(w, df.shape[1] * line + 4)
    cap = max_inches(o)
    return min(w, cap), min(h, cap)


def label_warning(df, o):
    """Message if labels will overlap even after enlarging."""
    line = o["font"] * 1.35 / 72
    if show_rows(df, o) and df.shape[0] * line + 3 > max_inches(o):
        fit = int((max_inches(o) - 3) / line)
        return (f"{df.shape[0]:,} row names cannot all fit at font {o['font']} and "
                f"plot height {max_inches(o)} in (about {fit:,} fit). Lower the font size "
                f"or use Top-N rows so the names don't overlap.")
    return None


def set_ticks(ax, df, o):
    if show_cols(df, o):
        ax.set_xticks(range(df.shape[1]))
        ax.set_xticklabels(df.columns, rotation=90, fontsize=o["font"])
    else:
        ax.set_xticks([])
    if show_rows(df, o):
        ax.set_yticks(range(df.shape[0]))
        ax.set_yticklabels(df.index, fontsize=o["font"])
    else:
        ax.set_yticks([])


def label_width_in(labels, font):
    """Rough width (inches) of the longest tick label."""
    return max((len(str(x)) for x in labels), default=0) * font * 0.62 / 72


def add_legend(fig, mappable, heat_ax, o, title, label_w=0.0, top_y=None):
    """Colour legend in its own space - never on top of the heatmap.
    Right: vertical bar beyond the row names. Top: horizontal bar above the plot.
    Sizes are in inches so the legend looks the same on any plot size."""
    W, H = fig.get_size_inches()
    pos = heat_ax.get_position()
    top = top_y if top_y is not None else pos.y1
    if o["legend"].startswith("Right"):
        bh = min(pos.height, 3.0 / H)   # aligned with the heatmap's top edge
        cax = fig.add_axes([pos.x1 + (label_w + 0.35) / W, pos.y1 - bh, 0.18 / W, bh])
        cb = fig.colorbar(mappable, cax=cax, orientation="vertical")
        title_y = top + 0.2 / H
    else:
        bw = min(pos.width, 3.0 / W)
        y0 = top + 0.3 / H
        cax = fig.add_axes([pos.x0, y0, bw, 0.15 / H])
        cb = fig.colorbar(mappable, cax=cax, orientation="horizontal")
        cax.xaxis.set_ticks_position("top")
        cax.xaxis.set_label_position("top")
        title_y = y0 + 0.15 / H + 0.6 / H
    cb.set_label(o["cbar_label"], fontsize=max(o["font"] + 2, 8))
    cb.ax.tick_params(labelsize=max(o["font"] + 1, 7))
    fig.text(pos.x0 + pos.width / 2, title_y, title, ha="center", va="bottom", fontsize=12)


def plot_plain(df, o):
    cmap, (vmin, vmax) = get_cmap(o), limits(df, o)
    fig, ax = plt.subplots(figsize=size(df, o))
    kw = ({"norm": TwoSlopeNorm(o["center"], vmin, vmax)} if o["center"] is not None
          else {"vmin": vmin, "vmax": vmax})
    im = ax.imshow(df.to_numpy(), aspect="auto", interpolation="nearest",
                   cmap=cmap, rasterized=True, **kw)
    set_ticks(ax, df, o)
    ax.set_xlabel(f"{df.shape[1]} columns"); ax.set_ylabel(f"{df.shape[0]} rows")
    add_legend(fig, im, ax, o, f"{o['title']} - without clustering")
    return fig


def plot_hier(df, o):
    from scipy.cluster.hierarchy import fcluster
    rc, cc = o["axis"] in ("rows", "both"), o["axis"] in ("columns", "both")
    vmin, vmax = limits(df, o)
    w, h = size(df, o)
    W, H = w + 2, h + 2
    rr, cr = min(0.2, 1.5 / W), min(0.2, 1.5 / H)   # dendrograms ~1.5 inch, any size
    g = sns.clustermap(df, method=o["method"], metric=o["metric"], cmap=get_cmap(o),
                       vmin=vmin, vmax=vmax, center=o["center"],
                       row_cluster=rc, col_cluster=cc, figsize=(W, H),
                       rasterized=True, dendrogram_ratio=(rr, cr),
                       cbar_pos=None,   # legend added by add_legend()
                       xticklabels=show_cols(df, o), yticklabels=show_rows(df, o))
    g.ax_heatmap.tick_params(labelsize=o["font"])
    if show_rows(df, o):
        g.ax_heatmap.set_yticklabels(g.ax_heatmap.get_yticklabels(), rotation=0)
    lw = label_width_in(df.index, o["font"]) if show_rows(df, o) else 0.0
    top = g.ax_col_dendrogram.get_position().y1 if cc else None
    add_legend(g.fig, g.ax_heatmap.collections[0], g.ax_heatmap, o,
               f"{o['title']} - hierarchical ({o['method']}, {o['metric']})",
               label_w=lw, top_y=top)
    table = None
    if rc:
        order = df.index[g.dendrogram_row.reordered_ind]
        table = pd.DataFrame({"row": order, "order": range(len(order))})
        if o["k"]:
            flat = fcluster(g.dendrogram_row.linkage, t=o["k"], criterion="maxclust")
            table["cluster"] = pd.Series(flat, index=df.index).loc[order].values
    return g.fig, table


def plot_kmeans(df, o):
    from sklearn.cluster import MiniBatchKMeans
    from scipy.cluster.hierarchy import linkage, leaves_list
    X, k = df.to_numpy(), o["k"]
    km = MiniBatchKMeans(n_clusters=k, random_state=42, n_init=5,
                         batch_size=min(4096, len(X))).fit(X)
    lab = km.labels_
    corder = leaves_list(linkage(km.cluster_centers_, "average")) if k > 2 else np.arange(k)
    rank = np.empty(k, int); rank[corder] = np.arange(k)
    dist = np.linalg.norm(X - km.cluster_centers_[lab], axis=1)
    ro = np.lexsort((dist, rank[lab]))
    d, lab = df.iloc[ro], lab[ro]
    if o["axis"] in ("columns", "both") and d.shape[1] > 2:
        d = d.iloc[:, leaves_list(linkage(d.T.to_numpy(), "average"))]
    vmin, vmax = limits(d, o)
    w, h = size(d, o)
    fig, (axc, ax) = plt.subplots(1, 2, figsize=(w + 0.5, h),
                                  gridspec_kw={"width_ratios": [0.03, 1], "wspace": 0.01})
    pal = plt.get_cmap("tab20" if k > 10 else "tab10")
    axc.imshow(lab[:, None], aspect="auto", cmap=pal, vmin=0, vmax=pal.N - 1,
               interpolation="nearest", rasterized=True)
    axc.set_xticks([])
    if show_rows(d, o):   # row names left of the cluster colour bar
        axc.set_yticks(range(d.shape[0]))
        axc.set_yticklabels(d.index, fontsize=o["font"])
    else:
        axc.set_yticks([])
    kw = ({"norm": TwoSlopeNorm(o["center"], vmin, vmax)} if o["center"] is not None
          else {"vmin": vmin, "vmax": vmax})
    im = ax.imshow(d.to_numpy(), aspect="auto", interpolation="nearest",
                   cmap=get_cmap(o), rasterized=True, **kw)
    for b in np.where(np.diff(lab) != 0)[0]:
        ax.axhline(b + 0.5, color="black", lw=0.6)
    set_ticks(ax, d, o); ax.set_yticks([])
    add_legend(fig, im, ax, o, f"{o['title']} - k-means (k={k})")
    return fig, pd.DataFrame({"row": d.index, "cluster": lab + 1})


MAX_PIXELS = 20000  # longest side of a PNG/TIFF/JPG download


def effective_dpi(fig, fmt, dpi):
    """Keep very tall plots fast and memory-safe.
    Images: limit pixels. PDF/SVG: text stays vector, only the colour cells
    are rasterised, 150 dpi is plenty for those."""
    big = max(fig.get_size_inches())
    if fmt in ("png", "tiff", "jpg"):
        return min(dpi, MAX_PIXELS / big)
    return min(dpi, 150) if big > 30 else dpi


def tight_box(fig):
    """Work out the trimmed page area once; reused by every save (faster)."""
    from matplotlib.transforms import Bbox
    bb = fig.get_tightbbox(fig.canvas.get_renderer())
    return Bbox.from_extents(bb.x0 - 0.1, bb.y0 - 0.1, bb.x1 + 0.1, bb.y1 + 0.1)


def fig_bytes(fig, fmt, dpi, box="tight"):
    b = io.BytesIO()
    fig.savefig(b, format=fmt, dpi=effective_dpi(fig, fmt, dpi), bbox_inches=box)
    return b.getvalue()


def download(col, label, data, file_name, key):
    try:   # Streamlit >= 1.43: clicking does not clear the page
        col.download_button(label, data, file_name=file_name, key=key, on_click="ignore")
    except TypeError:
        col.download_button(label, data, file_name=file_name, key=key)


def show_fig(fig, name, o, key):
    # light preview; downloads at chosen dpi (reduced only for very tall plots)
    big = max(fig.get_size_inches())
    box = tight_box(fig)
    png = fig_bytes(fig, "png", min(110, 6000 / big), box)
    try:
        st.image(png, width="stretch")          # newer Streamlit
    except Exception:
        st.image(png, use_container_width=True)  # older Streamlit
    cols = st.columns(len(o["formats"]))
    for c, fmt in zip(cols, o["formats"]):
        with st.spinner(f"Preparing {fmt.upper()}..."):
            data = fig_bytes(fig, fmt, o["dpi"], box)
        download(c, f"Download {fmt.upper()}", data, f"{name}.{fmt}", f"{key}_{fmt}")
    eff = effective_dpi(fig, "png", o["dpi"])
    if eff < o["dpi"] and any(f in ("png", "tiff", "jpg") for f in o["formats"]):
        st.caption(f"Plot is {big:.0f} in tall, so images are saved at {eff:.0f} DPI. "
                   "Use PDF or SVG for sharp row names at any zoom.")
    plt.close(fig)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.title("Heatmap Portal")
st.caption("Load a table, choose options, and plot heatmaps without clustering "
           "(supervised / original order) and with clustering (unsupervised).")

# ---- 1. data source -------------------------------------------------------- #
st.header("1. Load data")
src = st.radio("Source", ["Browse / upload file", "Local file path", "Demo data"],
               horizontal=True)
c1, c2, c3, c4 = st.columns(4)
sep_choice = c1.selectbox("Delimiter (text files)", ["auto", "comma ,", "tab \\t",
                                                      "semicolon ;", "space"])
header = c2.checkbox("First row = column names", True)
index_col = c3.checkbox("First column = row names", True)

raw, name, sheet = None, None, None
if src == "Browse / upload file":
    up = st.file_uploader("Choose a file", type=["csv", "tsv", "txt", "tab", "xlsx", "xls", "xlsm"])
    if up:
        raw, name = up.getvalue(), up.name
elif src == "Local file path":
    path = st.text_input("Full path to file (good for very large files)")
    if path:
        if os.path.isfile(path):
            with open(path, "rb") as f:
                raw, name = f.read(), os.path.basename(path)
        else:
            st.error("File not found.")

df_raw = None
try:
    if src == "Demo data":
        df_raw = demo_data()
    elif raw is not None:
        if name.lower().endswith((".xlsx", ".xls", ".xlsm")):
            sheet = c4.selectbox("Excel sheet", excel_sheets(raw))
        df_raw = read_table(raw, name, sep_choice, header, index_col, sheet)
except Exception as e:
    st.error(f"Could not read file: {e}")

if df_raw is None:
    st.info("Upload a file, enter a path, or pick Demo data to begin.")
    st.stop()

df_raw.index = df_raw.index.astype(str)
df_raw.columns = df_raw.columns.astype(str)
st.success(f"Loaded {df_raw.shape[0]:,} rows x {df_raw.shape[1]:,} columns")
with st.expander("Preview data", expanded=False):
    st.dataframe(df_raw.head(50))

all_cols = list(df_raw.select_dtypes(include=[np.number]).columns)
use_cols = st.multiselect("Numeric columns to plot", all_cols, default=all_cols)
if len(use_cols) < 2:
    st.warning("Select at least 2 numeric columns.")
    st.stop()

# ---- 2. sidebar options --------------------------------------------------- #
o = {}
sb = st.sidebar
sb.header("Preprocessing")
o["scale"] = sb.selectbox("Scaling", ["z-score rows", "none", "z-score columns", "min-max rows"])
o["log"] = sb.checkbox("log2(x + 1) transform", False)
o["nan"] = sb.selectbox("Missing values", ["fill row mean", "fill 0", "drop rows"])
o["top_n"] = sb.number_input("Keep top-N most variable rows (0 = all)", 0, None, 0, 500)
o["subsample"] = sb.number_input("Random subsample rows (0 = all)", 0, None, 0, 500)
o["clip"] = sb.number_input("Clip values to ± (0 = off)", 0.0, None, 0.0, 0.5)
o["transpose"] = sb.checkbox("Transpose (swap rows/columns)", False)

sb.header("Clustering (unsupervised)")
algo = sb.radio("Algorithm", ["Hierarchical", "K-means (very large data)"])
o["method"] = sb.selectbox("Linkage method", HIER_METHODS,
                           disabled=algo != "Hierarchical")
metric_opts = ["euclidean"] if o["method"] in EUCLID_ONLY else METRICS
o["metric"] = sb.selectbox("Distance metric", metric_opts,
                           disabled=algo != "Hierarchical",
                           help="ward / centroid / median require euclidean")
o["axis"] = sb.selectbox("Cluster", ["both", "rows", "columns"])
o["k"] = sb.slider("Number of clusters k (k-means / cut tree)", 2, 30, 6)
max_hier = sb.number_input("Max rows for hierarchical", 1000, 100000, 15000, 1000,
                           help="Memory grows ~ n²; above this switch to k-means or top-N")

sb.header("Colours")
o["custom"] = sb.checkbox("Custom colour gradient", False)
if o["custom"]:
    n = sb.slider("Number of colours", 2, 5, 3)
    defaults = ["#2166ac", "#f7f7f7", "#b2182b", "#fdae61", "#1a9850"]
    o["custom_colors"] = [sb.color_picker(f"Colour {i+1}", defaults[i], key=f"cc{i}")
                          for i in range(n)]
else:
    o["cmap"] = sb.selectbox("Colormap", POPULAR_CMAPS +
                             sorted(m for m in plt.colormaps()
                                    if not m.endswith("_r") and m not in POPULAR_CMAPS))
o["reverse"] = sb.checkbox("Reverse colours", False)
centred = sb.checkbox("Centre colour scale", o["scale"].startswith("z-score"))
o["center"] = sb.number_input("Centre value", value=0.0) if centred else None
o["robust"] = sb.checkbox("Robust limits (2–98 percentile)", True)
o["manual_limits"] = sb.checkbox("Set min / max manually", False)
if o["manual_limits"]:
    o["vmin"] = sb.number_input("Min", value=-3.0)
    o["vmax"] = sb.number_input("Max", value=3.0)

sb.header("Layout & export")
o["title"] = sb.text_input("Title", "Heatmap")
o["legend"] = sb.radio("Colour legend position", ["Right (vertical)", "Top (horizontal)"])
o["cbar_label"] = sb.text_input("Colour-bar label",
                                "z-score" if o["scale"].startswith("z-score") else "value")
o["width"] = sb.number_input("Width in inches (0 = auto)", 0.0, 60.0, 0.0)
o["height"] = sb.number_input("Height in inches (0 = auto)", 0.0, 60.0, 0.0)
o["font"] = sb.slider("Label font size", 3, 14, 6)
label_modes = ["Show all", "Auto (hide if too many)", "Hide"]
o["row_labels"] = sb.selectbox("Row names", label_modes)
o["col_labels"] = sb.selectbox("Column names", label_modes)
o["max_labels"] = sb.number_input("Auto: hide names if more than", 10, 100000, 200)
o["fit_labels"] = sb.checkbox("Enlarge plot so names don't overlap", True)
o["dpi"] = sb.selectbox("Download DPI", [150, 300, 600], index=1)
o["formats"] = sb.multiselect("Download formats", ["png", "pdf", "svg", "tiff"],
                              default=["png", "pdf"]) or ["png"]

# ---- 3. plot --------------------------------------------------------------- #
st.header("2. Plot")
what = st.radio("Show", ["Both", "Without clustering only", "With clustering only"],
                horizontal=True)
if not st.button("Plot heatmaps", type="primary"):
    st.stop()

with st.spinner("Preprocessing..."):
    df = preprocess(df_raw[use_cols], o)
if df.empty or df.shape[1] < 2:
    st.error("No data left after preprocessing - check scaling / missing-value options.")
    st.stop()
st.write(f"Plotting **{df.shape[0]:,} rows x {df.shape[1]:,} columns**")
if (msg := label_warning(df, o)):
    st.warning(msg)
if show_rows(df, o) and df.shape[0] > 150:
    st.info("Many row names: the preview is shrunk to fit the page. "
            "Download the PDF or SVG and zoom in to read every name.")

tabs = []
if what != "With clustering only":
    tabs.append("Without clustering")
if what != "Without clustering only":
    tabs.append("With clustering")
tab_objs = dict(zip(tabs, st.tabs(tabs)))

if "Without clustering" in tab_objs:
    with tab_objs["Without clustering"], st.spinner("Drawing heatmap..."):
        show_fig(plot_plain(df, o), "heatmap_no_clustering", o, "plain")

if "With clustering" in tab_objs:
    with tab_objs["With clustering"]:
        use_km = algo.startswith("K-means")
        if not use_km and o["axis"] in ("rows", "both") and df.shape[0] > max_hier:
            st.warning(f"{df.shape[0]:,} rows is above the hierarchical limit "
                       f"({max_hier:,}). Using k-means instead. Lower the rows with "
                       "top-N / subsample, or raise the limit, for a dendrogram.")
            use_km = True
        try:
            with st.spinner("Clustering..."):
                if use_km:
                    fig, table = plot_kmeans(df, o)
                    name = f"heatmap_kmeans_k{o['k']}"
                else:
                    fig, table = plot_hier(df, o)
                    name = f"heatmap_{o['method']}_{o['metric']}"
            show_fig(fig, name, o, "clust")
            if table is not None:
                st.subheader("Cluster assignments")
                if "cluster" in table:
                    st.write(table["cluster"].value_counts().sort_index()
                             .rename("rows").to_frame().T)
                st.dataframe(table, height=250)
                download(st, "Download clusters CSV", table.to_csv(index=False).encode(),
                         f"{name}_clusters.csv", "clusters_csv")
        except Exception as e:
            st.error(f"Clustering failed: {e}")
