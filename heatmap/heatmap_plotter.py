#!/usr/bin/env python3
"""
heatmap_plotter.py - Heatmaps for large data, with and without clustering.

Features
  * Input: CSV / TSV / TXT / Excel (rows = features/genes/samples, cols = conditions)
  * Large-data handling: top-N most variable rows, random subsampling,
    rasterized drawing, NaN handling, k-means pre-ordering for very large sets
  * Clustering options:
      - hierarchical: single, complete, average, weighted, centroid, median, ward
      - distance metrics: euclidean, correlation, cosine, cityblock, ... (any scipy metric)
      - kmeans: rows grouped by k-means clusters (scales to 100k+ rows)
      - cluster rows, columns, or both
  * Colour options: any matplotlib colormap, reversed maps, custom colour lists,
    centred (diverging) scales, robust min/max, or one plot per colormap
  * Saves: un-clustered heatmap + one clustered heatmap per method/metric/cmap combo
           in png / pdf / svg / tiff, plus cluster assignment tables

Examples
  python heatmap_plotter.py --demo
  python heatmap_plotter.py -i data.csv --index-col 0 --scale row
  python heatmap_plotter.py -i data.csv --methods average ward complete --metrics euclidean correlation
  python heatmap_plotter.py -i data.csv --cmaps viridis RdBu_r magma --center 0
  python heatmap_plotter.py -i data.csv --custom-colors navy white firebrick
  python heatmap_plotter.py -i big.csv --methods kmeans --k 8 --top-n 20000
  python heatmap_plotter.py -i data.csv --cluster-axis col --formats png pdf --dpi 600
"""

import argparse
import os
import sys
import warnings

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # no display needed
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm
import seaborn as sns

HIER_METHODS = ["single", "complete", "average", "weighted", "centroid", "median", "ward"]
EUCLID_ONLY = {"ward", "centroid", "median"}  # scipy requires euclidean for these
ALL_METHODS = HIER_METHODS + ["kmeans"]


# --------------------------------------------------------------------------- #
# Data loading & preprocessing
# --------------------------------------------------------------------------- #
def load_data(path, index_col, sheet, sep):
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, sheet_name=sheet or 0, index_col=index_col)
    else:
        if sep is None:
            sep = "\t" if ext in (".tsv", ".txt") else ","
        # pyarrow engine is much faster on big files if installed
        try:
            df = pd.read_csv(path, sep=sep, index_col=index_col, engine="pyarrow")
        except Exception:
            df = pd.read_csv(path, sep=sep, index_col=index_col, low_memory=False)
    return df


def make_demo(n_rows=5000, n_cols=40, n_groups=5, seed=0):
    """Synthetic data with hidden row/column structure."""
    rng = np.random.default_rng(seed)
    row_grp = rng.integers(0, n_groups, n_rows)
    col_grp = rng.integers(0, 4, n_cols)
    pattern = rng.normal(0, 2, (n_groups, 4))
    data = pattern[row_grp][:, col_grp] + rng.normal(0, 1, (n_rows, n_cols))
    return pd.DataFrame(data,
                        index=[f"feature_{i}" for i in range(n_rows)],
                        columns=[f"sample_{j}" for j in range(n_cols)])


def preprocess(df, args):
    # keep numeric columns only
    num = df.select_dtypes(include=[np.number])
    dropped = df.shape[1] - num.shape[1]
    if dropped:
        print(f"[info] dropped {dropped} non-numeric column(s)")
    df = num.astype(np.float32)

    # missing values
    if args.nan == "drop":
        df = df.dropna(axis=0, how="any")
    elif args.nan == "zero":
        df = df.fillna(0)
    elif args.nan == "mean":
        df = df.apply(lambda r: r.fillna(r.mean()), axis=1)
    df = df.loc[df.notna().any(axis=1)]

    if args.log:
        df = np.log2(df.clip(lower=0) + args.log_pseudo)

    # drop constant rows (break correlation distance / z-score)
    df = df.loc[df.std(axis=1) > 0]

    # reduce size: most variable rows first, then random subsample
    if args.top_n and df.shape[0] > args.top_n:
        top = df.var(axis=1).nlargest(args.top_n).index
        df = df.loc[top]
        print(f"[info] kept top {args.top_n} most variable rows")
    if args.subsample and df.shape[0] > args.subsample:
        df = df.sample(n=args.subsample, random_state=args.seed)
        print(f"[info] randomly subsampled to {args.subsample} rows")

    # scaling
    if args.scale == "row":
        df = df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)
    elif args.scale == "col":
        df = (df - df.mean()) / df.std()
    elif args.scale == "minmax":
        mn, mx = df.min(axis=1), df.max(axis=1)
        df = df.sub(mn, axis=0).div(mx - mn, axis=0)

    if args.clip is not None:
        df = df.clip(-args.clip, args.clip)
    if args.transpose:
        df = df.T
    return df


# --------------------------------------------------------------------------- #
# Colours
# --------------------------------------------------------------------------- #
def build_cmaps(args):
    """Return list of (name, colormap) pairs."""
    cmaps = []
    if args.custom_colors:
        name = "custom_" + "-".join(c.strip("#") for c in args.custom_colors)
        cmaps.append((name, LinearSegmentedColormap.from_list(name, args.custom_colors, N=256)))
    for c in args.cmaps:
        try:
            cmaps.append((c, plt.get_cmap(c)))
        except ValueError:
            print(f"[warn] unknown colormap '{c}', skipped")
    if not cmaps:
        cmaps = [("RdBu_r", plt.get_cmap("RdBu_r"))]
    return cmaps


def color_limits(df, args):
    vals = df.to_numpy()
    if args.vmin is not None or args.vmax is not None:
        vmin = args.vmin if args.vmin is not None else np.nanmin(vals)
        vmax = args.vmax if args.vmax is not None else np.nanmax(vals)
    elif args.robust:
        vmin, vmax = np.nanpercentile(vals, [2, 98])
    else:
        vmin, vmax = np.nanmin(vals), np.nanmax(vals)
    if args.center is not None:  # symmetric around centre
        span = max(abs(vmax - args.center), abs(args.center - vmin))
        vmin, vmax = args.center - span, args.center + span
    return float(vmin), float(vmax)


# --------------------------------------------------------------------------- #
# Plot helpers
# --------------------------------------------------------------------------- #
def tick_settings(n, max_labels):
    """Show labels only when readable."""
    if n <= max_labels:
        return True
    return False


def fig_size(df, args):
    if args.width and args.height:
        return args.width, args.height
    w = min(max(6, df.shape[1] * 0.25 + 3), 30)
    h = min(max(6, df.shape[0] * 0.12 + 2), 30)
    return w, h


def save(fig, outbase, args):
    for fmt in args.formats:
        path = f"{outbase}.{fmt}"
        fig.savefig(path, dpi=args.dpi, bbox_inches="tight")
        print(f"[saved] {path}")
    plt.close(fig)


def plot_plain(df, cmap_name, cmap, vmin, vmax, args, outdir):
    """Heatmap in original order (no clustering)."""
    w, h = fig_size(df, args)
    fig, ax = plt.subplots(figsize=(w, h))
    norm = TwoSlopeNorm(args.center, vmin, vmax) if args.center is not None else None
    im = ax.imshow(df.to_numpy(), aspect="auto", interpolation="nearest",
                   cmap=cmap, vmin=None if norm else vmin, vmax=None if norm else vmax,
                   norm=norm, rasterized=True)
    if tick_settings(df.shape[1], args.max_labels):
        ax.set_xticks(range(df.shape[1]))
        ax.set_xticklabels(df.columns, rotation=90, fontsize=args.font_size)
    else:
        ax.set_xticks([])
    if tick_settings(df.shape[0], args.max_labels):
        ax.set_yticks(range(df.shape[0]))
        ax.set_yticklabels(df.index, fontsize=args.font_size)
    else:
        ax.set_yticks([])
    ax.set_xlabel(f"{df.shape[1]} columns")
    ax.set_ylabel(f"{df.shape[0]} rows")
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.set_label(args.cbar_label)
    ax.set_title(f"{args.title} (no clustering)")
    save(fig, os.path.join(outdir, f"{args.prefix}_noclust_{cmap_name}"), args)


def kmeans_order(df, k, seed, axis_df):
    """Order rows of axis_df by k-means cluster, then by distance to centroid."""
    from sklearn.cluster import MiniBatchKMeans
    X = axis_df.to_numpy()
    km = MiniBatchKMeans(n_clusters=k, random_state=seed, n_init=5,
                         batch_size=min(4096, len(X))).fit(X)
    labels = km.labels_
    # order clusters themselves by hierarchical clustering of centroids
    if k > 2:
        from scipy.cluster.hierarchy import linkage, leaves_list
        corder = leaves_list(linkage(km.cluster_centers_, "average"))
    else:
        corder = np.arange(k)
    rank = {c: i for i, c in enumerate(corder)}
    dist = np.linalg.norm(X - km.cluster_centers_[labels], axis=1)
    order = np.lexsort((dist, [rank[l] for l in labels]))
    return order, labels


def plot_kmeans(df, cmap_name, cmap, vmin, vmax, args, outdir):
    """K-means grouped heatmap - scales to very large row counts."""
    row_order, labels = kmeans_order(df, args.k, args.seed, df)
    d = df.iloc[row_order]
    lab = labels[row_order]
    # optional hierarchical clustering of the (few) columns
    if args.cluster_axis in ("col", "both") and df.shape[1] > 2:
        from scipy.cluster.hierarchy import linkage, leaves_list
        corder = leaves_list(linkage(d.T.to_numpy(), "average", metric="euclidean"))
        d = d.iloc[:, corder]

    w, h = fig_size(d, args)
    fig, (axc, ax) = plt.subplots(1, 2, figsize=(w + 0.5, h), sharey=True,
                                  gridspec_kw={"width_ratios": [0.03, 1], "wspace": 0.01})
    pal = plt.get_cmap("tab20" if args.k > 10 else "tab10")
    axc.imshow(lab[:, None], aspect="auto", cmap=pal, interpolation="nearest",
               vmin=0, vmax=pal.N - 1, rasterized=True)
    axc.set_xticks([]); axc.set_yticks([])
    norm = TwoSlopeNorm(args.center, vmin, vmax) if args.center is not None else None
    im = ax.imshow(d.to_numpy(), aspect="auto", interpolation="nearest", cmap=cmap,
                   vmin=None if norm else vmin, vmax=None if norm else vmax,
                   norm=norm, rasterized=True)
    # cluster boundaries
    for b in np.where(np.diff(lab) != 0)[0]:
        ax.axhline(b + 0.5, color="black", lw=0.6)
    if d.shape[1] <= args.max_labels:
        ax.set_xticks(range(d.shape[1]))
        ax.set_xticklabels(d.columns, rotation=90, fontsize=args.font_size)
    else:
        ax.set_xticks([])
    ax.set_yticks([])
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.set_label(args.cbar_label)
    ax.set_title(f"{args.title} (k-means, k={args.k})")
    tag = f"{args.prefix}_clust_kmeans_k{args.k}_{cmap_name}"
    save(fig, os.path.join(outdir, tag), args)
    pd.DataFrame({"row": d.index, "cluster": lab}).to_csv(
        os.path.join(outdir, f"{tag}_clusters.csv"), index=False)


def plot_hier(df, method, metric, cmap_name, cmap, vmin, vmax, args, outdir):
    """Hierarchical clustering with dendrograms (seaborn clustermap)."""
    from scipy.cluster.hierarchy import fcluster
    row_c = args.cluster_axis in ("row", "both")
    col_c = args.cluster_axis in ("col", "both")
    if row_c and df.shape[0] > args.max_hier_rows:
        print(f"[warn] {df.shape[0]} rows > --max-hier-rows {args.max_hier_rows}; "
              f"skipping {method}/{metric}. Use --top-n, --subsample or --methods kmeans.")
        return
    w, h = fig_size(df, args)
    try:
        g = sns.clustermap(
            df, method=method, metric=metric, cmap=cmap,
            vmin=vmin, vmax=vmax, center=args.center,
            row_cluster=row_c, col_cluster=col_c,
            figsize=(w + 2, h + 2), rasterized=True,
            xticklabels=df.shape[1] <= args.max_labels,
            yticklabels=df.shape[0] <= args.max_labels,
            dendrogram_ratio=(0.12, 0.12),
            cbar_kws={"label": args.cbar_label},
        )
    except Exception as e:
        print(f"[warn] {method}/{metric} failed: {e}")
        return
    g.ax_heatmap.tick_params(labelsize=args.font_size)
    # move colour legend to the right of the row names so it never covers the plot
    W, H = g.fig.get_size_inches()
    pos = g.ax_heatmap.get_position()
    lw = (max(len(str(x)) for x in df.index) * args.font_size * 0.62 / 72
          if df.shape[0] <= args.max_labels else 0)
    bh = min(pos.height, 3.0 / H)
    g.ax_cbar.set_position([pos.x1 + (lw + 0.35) / W, pos.y1 - bh, 0.18 / W, bh])
    g.fig.suptitle(f"{args.title} (hierarchical: {method}, {metric})", y=1.02)
    tag = f"{args.prefix}_clust_{method}_{metric}_{cmap_name}"
    save(g.fig, os.path.join(outdir, tag), args)

    # export cluster order (+ flat clusters if requested)
    if row_c:
        link = g.dendrogram_row.linkage
        ordered = df.index[g.dendrogram_row.reordered_ind]
        out = pd.DataFrame({"row": ordered, "order": range(len(ordered))})
        if args.k:
            flat = fcluster(link, t=args.k, criterion="maxclust")
            out["cluster"] = pd.Series(flat, index=df.index).loc[ordered].values
        out.to_csv(os.path.join(outdir, f"{tag}_row_order.csv"), index=False)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    io = p.add_argument_group("input / output")
    io.add_argument("-i", "--input", help="CSV/TSV/TXT/XLSX file")
    io.add_argument("--demo", action="store_true", help="run on synthetic data")
    io.add_argument("--demo-rows", type=int, default=5000)
    io.add_argument("--index-col", type=int, default=0, help="row-name column (-1 = none)")
    io.add_argument("--sep", default=None, help="delimiter (auto by extension)")
    io.add_argument("--sheet", default=None, help="Excel sheet name")
    io.add_argument("-o", "--outdir", default="heatmaps")
    io.add_argument("--prefix", default="heatmap")
    io.add_argument("--formats", nargs="+", default=["png"],
                    choices=["png", "pdf", "svg", "tiff", "jpg"])
    io.add_argument("--dpi", type=int, default=300)

    pre = p.add_argument_group("preprocessing / large data")
    pre.add_argument("--top-n", type=int, default=None, help="keep N most variable rows")
    pre.add_argument("--subsample", type=int, default=None, help="random N rows")
    pre.add_argument("--scale", choices=["none", "row", "col", "minmax"], default="none",
                     help="row/col = z-score")
    pre.add_argument("--log", action="store_true", help="log2(x + pseudo)")
    pre.add_argument("--log-pseudo", type=float, default=1.0)
    pre.add_argument("--clip", type=float, default=None, help="clip values to [-c, c]")
    pre.add_argument("--nan", choices=["drop", "zero", "mean"], default="mean")
    pre.add_argument("--transpose", action="store_true")
    pre.add_argument("--seed", type=int, default=42)

    cl = p.add_argument_group("clustering")
    cl.add_argument("--methods", nargs="+", default=["average"],
                    help=f"any of {ALL_METHODS} or 'all'")
    cl.add_argument("--metrics", nargs="+", default=["euclidean"],
                    help="scipy distance metrics: euclidean correlation cosine cityblock "
                         "chebyshev braycurtis canberra ...")
    cl.add_argument("--cluster-axis", choices=["row", "col", "both"], default="both")
    cl.add_argument("--k", type=int, default=6,
                    help="clusters for kmeans / flat clusters cut from dendrogram (0 = none)")
    cl.add_argument("--max-hier-rows", type=int, default=15000,
                    help="hierarchical clustering limit (memory ~ n^2)")
    cl.add_argument("--no-plain", action="store_true", help="skip un-clustered plot")
    cl.add_argument("--no-cluster", action="store_true", help="skip clustered plots")

    co = p.add_argument_group("colours")
    co.add_argument("--cmaps", nargs="+", default=["RdBu_r"],
                    help="matplotlib colormaps, e.g. viridis magma coolwarm RdYlBu_r "
                         "bwr seismic YlOrRd Blues (add _r to reverse)")
    co.add_argument("--custom-colors", nargs="+", default=None,
                    help="build a gradient, e.g. navy white firebrick or '#2166ac' '#f7f7f7' '#b2182b'")
    co.add_argument("--center", type=float, default=None, help="centre of diverging scale")
    co.add_argument("--vmin", type=float, default=None)
    co.add_argument("--vmax", type=float, default=None)
    co.add_argument("--robust", action="store_true", help="limits from 2nd/98th percentiles")
    co.add_argument("--list-cmaps", action="store_true", help="print colormap names and exit")

    lo = p.add_argument_group("layout")
    lo.add_argument("--title", default="Heatmap")
    lo.add_argument("--cbar-label", default="value")
    lo.add_argument("--width", type=float, default=None)
    lo.add_argument("--height", type=float, default=None)
    lo.add_argument("--font-size", type=float, default=6)
    lo.add_argument("--max-labels", type=int, default=100,
                    help="hide tick labels when an axis has more items than this")
    return p.parse_args()


def main():
    args = parse_args()
    if args.list_cmaps:
        print("\n".join(sorted(m for m in plt.colormaps() if not m.endswith("_r"))))
        print("\n(append _r to any name to reverse it)")
        return
    if not args.input and not args.demo:
        sys.exit("Provide --input FILE or --demo (see --help)")

    warnings.filterwarnings("ignore", category=UserWarning)
    os.makedirs(args.outdir, exist_ok=True)

    if args.demo:
        df = make_demo(n_rows=args.demo_rows, seed=args.seed)
        args.scale = args.scale if args.scale != "none" else "row"
    else:
        df = load_data(args.input, None if args.index_col < 0 else args.index_col,
                       args.sheet, args.sep)
    print(f"[info] loaded {df.shape[0]} rows x {df.shape[1]} cols")
    df = preprocess(df, args)
    print(f"[info] plotting {df.shape[0]} rows x {df.shape[1]} cols")
    if df.empty:
        sys.exit("No data left after preprocessing.")
    if args.scale in ("row", "col") and args.center is None:
        args.center = 0.0
        args.cbar_label = "z-score" if args.cbar_label == "value" else args.cbar_label

    cmaps = build_cmaps(args)
    vmin, vmax = color_limits(df, args)
    methods = ALL_METHODS if "all" in args.methods else args.methods

    for cmap_name, cmap in cmaps:
        if not args.no_plain:
            plot_plain(df, cmap_name, cmap, vmin, vmax, args, args.outdir)
        if args.no_cluster:
            continue
        for method in methods:
            if method == "kmeans":
                plot_kmeans(df, cmap_name, cmap, vmin, vmax, args, args.outdir)
                continue
            if method not in HIER_METHODS:
                print(f"[warn] unknown method '{method}', skipped")
                continue
            for metric in args.metrics:
                if method in EUCLID_ONLY and metric != "euclidean":
                    print(f"[info] {method} needs euclidean; skipping metric '{metric}'")
                    continue
                plot_hier(df, method, metric, cmap_name, cmap, vmin, vmax, args, args.outdir)

    print(f"\nDone. Output in: {os.path.abspath(args.outdir)}")


if __name__ == "__main__":
    main()
