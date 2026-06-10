"""Интерактивное демо для PAUK: визуализация связей по лабораториям ИТМО.

Три вкладки:
  1) «Лаборатория» — карточка одной из выбранных лаб: счётчики, список репо,
     граф авторы ↔ публикации ↔ репозитории.
  2) «Сравнение» — параллельные метрики и Sankey-диаграмма потоков
     «автор → публикация → репо» для всех выбранных лаб.
  3) «Сеть» — единый граф всех авторов и репо обеих лаб, в нём видны
     общие соавторы и пересечения экосистем.

Запуск из корня проекта:
    uv run streamlit run app.py
"""

import sqlite3
from pathlib import Path

import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "data" / "itmo_research_opensource.db"


LABS: dict[str, dict] = {
    "ITMO-NSS-team": {
        "title": "ITMO NSS Team",
        "subtitle": "Natural Systems Simulation Lab",
    },
    "aimclub": {
        "title": "AIM Club",
        "subtitle": "Городские модели и эволюционные алгоритмы",
    },
    "antigenomics": {
        "title": "Antigenomics",
        "subtitle": "Иммуногеномика, репертуары T-клеток",
    },
}

LAB_COLORS = {
    "ITMO-NSS-team": "#3182bd",
    "aimclub": "#e6550d",
    "antigenomics": "#74c476",
}


# ----------------------------- ЗАГРУЗКА ДАННЫХ -----------------------------


@st.cache_data
def load_links() -> pd.DataFrame:
    """Все строки repo_links с метаданными публикаций, плюс лейбл лабы по URL."""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT r.id          AS link_id,
               r.publication_id,
               r.url,
               r.host,
               r.is_relevant,
               r.llm_confidence,
               r.llm_reason,
               p.title,
               p.doi,
               p.year,
               p.publication_date,
               p.openalex_url
        FROM repo_links r
        JOIN publications p ON p.id = r.publication_id
        """,
        conn,
    )
    conn.close()
    df["lab"] = df["url"].map(extract_lab)
    df["repo_name"] = df["url"].str.replace("https://github.com/", "", regex=False)
    return df


@st.cache_data
def load_authors_for(publication_ids: tuple[str, ...]) -> pd.DataFrame:
    """Авторы выбранных публикаций (ИТМО + внешние)."""
    if not publication_ids:
        return pd.DataFrame(
            columns=["publication_id", "name", "person_type", "position"]
        )
    placeholders = ",".join("?" * len(publication_ids))
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        f"""
        SELECT pa.publication_id,
               COALESCE(pi.name_en, pe.name_en, 'Unknown') AS name,
               pa.person_type,
               pa.author_position AS position
        FROM publication_authors pa
        LEFT JOIN persons_itmo     pi ON pi.id = pa.person_id AND pa.person_type='itmo'
        LEFT JOIN persons_external pe ON pe.id = pa.person_id AND pa.person_type='external'
        WHERE pa.publication_id IN ({placeholders})
        ORDER BY pa.publication_id, pa.author_position
        """,
        conn,
        params=list(publication_ids),
    )
    conn.close()
    return df


def extract_lab(url: str | None) -> str | None:
    """Возвращает GitHub-организацию (часть после github.com/) для любой ссылки.

    Используется как «принадлежность к группе» — даже если организация не входит
    в LABS, мы её сохраняем, чтобы потом отрисовать нейтральным цветом.
    """
    if not url or "github.com/" not in url:
        return None
    return url.split("github.com/", 1)[1].split("/", 1)[0]


NEUTRAL_LAB_COLOR = "#666666"  # серый для не-подсвеченных org


def color_for(lab: str | None) -> str:
    """Цвет узла/потока по принадлежности к лаборатории."""
    if lab is None:
        return NEUTRAL_LAB_COLOR
    return LAB_COLORS.get(lab, NEUTRAL_LAB_COLOR)


# ----------------------------- ВКЛАДКА 1: ЛАБА -----------------------------


def render_lab_card(lab_key: str, df_links: pd.DataFrame) -> None:
    df_lab = df_links[df_links["lab"] == lab_key].copy()
    meta = LABS.get(lab_key) or {
        "title": lab_key,
        "subtitle": "Организация без отдельной подсветки",
    }

    st.subheader(f"{meta['title']}")
    st.caption(meta["subtitle"])

    pub_ids = df_lab["publication_id"].unique().tolist()
    n_pubs = len(pub_ids)
    n_repos = df_lab["url"].nunique()
    n_confirmed = int((df_lab["is_relevant"] == 1).sum())

    df_authors = load_authors_for(tuple(pub_ids))
    n_itmo_authors = df_authors[df_authors["person_type"] == "itmo"]["name"].nunique()
    n_ext_authors = df_authors[df_authors["person_type"] == "external"][
        "name"
    ].nunique()

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Публикаций", n_pubs)
    c2.metric("Уникальных репо", n_repos)
    c3.metric("Авторских", n_confirmed)
    c4.metric("ИТМО-авторов", n_itmo_authors)
    c5.metric("Внешних авторов", n_ext_authors)

    if n_pubs == 0:
        st.warning("Для этой лаборатории нет данных в repo_links.")
        return

    st.markdown("#### Репозитории и публикации")
    show = df_lab[["repo_name", "title", "year", "is_relevant"]].copy()
    show["статус"] = (
        show["is_relevant"].map({1: "Авторский", 0: "Неподтверждённый"}).fillna("—")
    )
    show = show[["repo_name", "title", "year", "статус"]]
    show.columns = ["репозиторий", "статья", "год", "статус"]
    st.dataframe(show, width="stretch", hide_index=True)

    st.markdown("#### Граф: авторы, публикации и репозитории")
    c1, c2 = st.columns([1, 2])
    with c1:
        show_labels = st.toggle(
            "Показывать подписи", value=True, key=f"labels_lab_{lab_key}"
        )
    with c2:
        kinds = st.multiselect(
            "Показывать на графе",
            options=["публикации", "репо", "ИТМО-авторы", "внешние авторы"],
            default=["публикации", "репо", "ИТМО-авторы", "внешние авторы"],
            key=f"kinds_lab_{lab_key}",
        )
    fig = build_author_paper_repo_graph(
        df_lab,
        df_authors,
        color=color_for(lab_key),
        show_labels=show_labels,
        kinds=kinds,
    )
    st.plotly_chart(fig, width="stretch")


def _build_edge_traces(G: nx.Graph, pos: dict) -> list[go.Scatter]:
    """Возвращает три trace для рёбер: нейтральные, авторские, неподтверждённые.

    Различие в стиле — для is_relevant=1 сплошная зелёная,
    для is_relevant=0 пунктирная красноватая, для остальных серая сплошная.
    """
    groups = {
        "neutral": {"x": [], "y": [], "dash": "solid", "color": "#777777", "name": "связь"},
        "authored": {
            "x": [], "y": [],
            "dash": "solid", "color": "#31a354", "name": "ребро к авторскому репо",
        },
        "rejected": {
            "x": [], "y": [],
            "dash": "dash", "color": "#de2d26", "name": "ребро к неподтверждённому",
        },
    }
    for u, v, data in G.edges(data=True):
        ir = data.get("is_relevant")
        bucket = "authored" if ir == 1 else "rejected" if ir == 0 else "neutral"
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        groups[bucket]["x"] += [x0, x1, None]
        groups[bucket]["y"] += [y0, y1, None]
    traces: list[go.Scatter] = []
    for key, g in groups.items():
        if not g["x"]:
            continue
        traces.append(
            go.Scatter(
                x=g["x"],
                y=g["y"],
                line=dict(width=1.0, color=g["color"], dash=g["dash"]),
                hoverinfo="none",
                mode="lines",
                name=g["name"],
                showlegend=key != "neutral",
            )
        )
    return traces


def build_author_paper_repo_graph(
    df_links: pd.DataFrame,
    df_authors: pd.DataFrame,
    color: str,
    show_labels: bool = True,
    kinds: list[str] | None = None,
) -> go.Figure:
    if kinds is None:
        kinds = ["публикации", "репо", "ИТМО-авторы", "внешние авторы"]
    show_papers = "публикации" in kinds
    show_repos = "репо" in kinds
    show_itmo = "ИТМО-авторы" in kinds
    show_ext = "внешние авторы" in kinds
    G = nx.Graph()

    for pid in df_links["publication_id"].unique():
        G.add_node(("paper", pid), kind="paper", label=pid)
    for _, row in df_links.iterrows():
        G.add_node(("repo", row["url"]), kind="repo", label=row["repo_name"])
        G.add_edge(
            ("paper", row["publication_id"]),
            ("repo", row["url"]),
            is_relevant=row.get("is_relevant"),
        )
    for _, row in df_authors.iterrows():
        G.add_node(
            ("author", row["name"]),
            kind="author",
            label=row["name"],
            person_type=row["person_type"],
        )
        G.add_edge(("paper", row["publication_id"]), ("author", row["name"]))

    # Удаляем узлы по фильтрам — связанные рёбра NetworkX уносит за собой.
    to_remove = []
    for node, data in G.nodes(data=True):
        kind = data["kind"]
        if kind == "paper" and not show_papers:
            to_remove.append(node)
        elif kind == "repo" and not show_repos:
            to_remove.append(node)
        elif kind == "author":
            if data.get("person_type") == "itmo" and not show_itmo:
                to_remove.append(node)
            elif data.get("person_type") != "itmo" and not show_ext:
                to_remove.append(node)
    G.remove_nodes_from(to_remove)

    if G.number_of_nodes() == 0:
        return go.Figure()

    pos = nx.spring_layout(G, seed=42, k=1.2 / (len(G.nodes) ** 0.5))

    traces: list[go.Scatter] = _build_edge_traces(G, pos)
    styles = {
        "paper": dict(
            symbol="square",
            size=14,
            color="#9ecae1",
            border="#3182bd",
            name="публикация",
        ),
        "repo": dict(
            symbol="diamond", size=18, color=color, border="black", name="репозиторий"
        ),
        "author_itmo": dict(
            symbol="circle",
            size=12,
            color="#fee391",
            border="#cc4c02",
            name="автор ИТМО",
        ),
        "author_external": dict(
            symbol="circle",
            size=10,
            color="#f0f0f0",
            border="#969696",
            name="внешний автор",
        ),
    }

    visible_labels = {"repo", "author_itmo"} if show_labels else set()

    for style_key, style in styles.items():
        xs, ys, texts, visible_texts = [], [], [], []
        for node, data in G.nodes(data=True):
            kind = data["kind"]
            if kind == "author":
                k = (
                    "author_itmo"
                    if data.get("person_type") == "itmo"
                    else "author_external"
                )
            else:
                k = kind
            if k != style_key:
                continue
            x, y = pos[node]
            xs.append(x)
            ys.append(y)
            texts.append(data["label"])
            label = data["label"]
            if kind == "repo" and "/" in label:
                label = label.split("/", 1)[1]
            visible_texts.append(label)
        if not xs:
            continue
        show_text = style_key in visible_labels
        traces.append(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers+text" if show_text else "markers",
                text=visible_texts if show_text else None,
                textposition="top center",
                textfont=dict(size=11, color="#f0f0f0"),
                hovertext=texts,
                hoverinfo="text",
                name=style["name"],
                marker=dict(
                    symbol=style["symbol"],
                    size=style["size"],
                    color=style["color"],
                    line=dict(color=style["border"], width=1.5),
                ),
            )
        )

    fig = go.Figure(data=traces)
    fig.update_layout(
        showlegend=True,
        hovermode="closest",
        margin=dict(b=20, l=5, r=5, t=20),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        height=550,
    )
    return fig


# ----------------------------- ВКЛАДКА 2: СРАВНЕНИЕ ------------------------


def render_comparison(df_links: pd.DataFrame) -> None:
    df_all = df_links[df_links["lab"].notna()].copy()

    # Топ-15 организаций по числу публикаций.
    org_pub_counts = (
        df_all.groupby("lab")["publication_id"]
        .nunique()
        .sort_values(ascending=False)
    )
    top_orgs = org_pub_counts.head(15).index.tolist()
    df_top = df_all[df_all["lab"].isin(top_orgs)]

    rows = []
    for lab_key in top_orgs:
        df_lab = df_top[df_top["lab"] == lab_key]
        pub_ids = df_lab["publication_id"].unique().tolist()
        df_authors = load_authors_for(tuple(pub_ids))
        title = LABS.get(lab_key, {}).get("title", lab_key)
        marker = "★ " if lab_key in LABS else ""
        rows.append(
            {
                "Лаборатория": f"{marker}{title}",
                "Публикаций": len(pub_ids),
                "Уникальных репо": df_lab["url"].nunique(),
                "Авторских": int((df_lab["is_relevant"] == 1).sum()),
                "ИТМО-авторов": df_authors[df_authors["person_type"] == "itmo"][
                    "name"
                ].nunique(),
                "Внешних авторов": df_authors[df_authors["person_type"] == "external"][
                    "name"
                ].nunique(),
            }
        )
    df_summary = pd.DataFrame(rows).set_index("Лаборатория")
    st.markdown("#### Сводная таблица — топ-15 GitHub-организаций")
    st.caption(
        "★ — организации с заранее заданным цветом (то, что мы используем "
        "в графах). Остальные присутствуют в общем графе серым."
    )
    st.dataframe(df_summary, width="stretch")

    st.markdown("#### Sankey: потоки между лабами, авторами, публикациями и репо")
    mode_label = st.radio(
        "Что ставим в центр потока",
        options=[
            "human-first (автор)",
            "paper-first (публикация)",
            "repo-first (репозиторий)",
        ],
        horizontal=True,
        help=(
            "human-first — lab→автор→публикация→репо (полная цепочка). "
            "paper-first — lab→публикация→автор (без репо, фокус на статьях). "
            "repo-first — lab→репо→автор (без публикаций, видно «кто пользуется этим репо»)."
        ),
    )
    mode = mode_label.split("-")[0]  # 'human' / 'paper' / 'repo'
    fig = build_sankey(df_links, mode=mode)
    st.plotly_chart(fig, width="stretch")


def build_sankey(df_links: pd.DataFrame, mode: str = "human") -> go.Figure:
    nodes: list[str] = []
    node_index: dict[str, int] = {}

    def add(label: str) -> int:
        if label not in node_index:
            node_index[label] = len(nodes)
            nodes.append(label)
        return node_index[label]

    SANKEY_LINK_NEUTRAL = "rgba(180,180,180,0.35)"
    SANKEY_LINK_AUTHORED = "rgba(49,163,84,0.55)"   # зелёный, авторский
    SANKEY_LINK_REJECTED = "rgba(222,45,38,0.45)"   # красный, неподтверждённый

    def link_color(is_relevant) -> str:
        if is_relevant == 1:
            return SANKEY_LINK_AUTHORED
        if is_relevant == 0:
            return SANKEY_LINK_REJECTED
        return SANKEY_LINK_NEUTRAL

    def edge(s: str, d: str, is_relevant=None) -> None:
        src.append(add(s))
        dst.append(add(d))
        val.append(1)
        link_colors.append(link_color(is_relevant))

    src, dst, val, link_colors = [], [], [], []

    df_lab_links = df_links[df_links["lab"].notna()].copy()
    # Топ-N организаций по числу публикаций (защита от каши в Sankey).
    top_orgs = (
        df_lab_links.groupby("lab")["publication_id"]
        .nunique()
        .sort_values(ascending=False)
        .head(15)
        .index.tolist()
    )
    df_lab_links = df_lab_links[df_lab_links["lab"].isin(top_orgs)]
    pub_ids = df_lab_links["publication_id"].unique().tolist()
    df_authors = load_authors_for(tuple(pub_ids))
    itmo_authors = df_authors[df_authors["person_type"] == "itmo"]

    for lab_key in LABS:
        lab_label = LABS[lab_key]["title"]
        df_lab = df_lab_links[df_lab_links["lab"] == lab_key]
        pub_authors: dict[str, list[str]] = {}
        for pub_id in df_lab["publication_id"].unique():
            names = itmo_authors[itmo_authors["publication_id"] == pub_id][
                "name"
            ].tolist()
            pub_authors[pub_id] = names or ["(нет ITMO-автора)"]

        if mode == "human":
            # lab -> автор -> публикация -> репо  (полная цепочка)
            for pub_id, authors in pub_authors.items():
                for author in authors:
                    edge(lab_label, f"👤 {author}")
                    edge(f"👤 {author}", f"📄 {pub_id}")
            for _, row in df_lab.iterrows():
                edge(
                    f"📄 {row['publication_id']}",
                    f"📦 {row['repo_name']}",
                    is_relevant=row.get("is_relevant"),
                )

        elif mode == "paper":
            # lab -> публикация -> автор  (репо убраны, фокус на статьях)
            for pub_id, authors in pub_authors.items():
                edge(lab_label, f"📄 {pub_id}")
                for author in authors:
                    edge(f"📄 {pub_id}", f"👤 {author}")

        else:  # mode == "repo"
            # lab -> репо -> автор  (публикации убраны, repo→author — через общие пабы)
            for _, row in df_lab.iterrows():
                repo_label = f"📦 {row['repo_name']}"
                edge(lab_label, repo_label, is_relevant=row.get("is_relevant"))
                authors_of_pub = pub_authors.get(row["publication_id"], [])
                for author in authors_of_pub:
                    edge(repo_label, f"👤 {author}", is_relevant=row.get("is_relevant"))

    # Цвет узла подбираем по префиксу — лабы, авторы, статьи, репо
    # имеют разный визуальный «вес».
    palette = {
        "lab": "#3182bd",
        "author": "#fdae6b",
        "paper": "#bdbdbd",
        "repo": "#74c476",
    }
    node_colors = []
    for label in nodes:
        if label.startswith("👤 "):
            node_colors.append(palette["author"])
        elif label.startswith("📄 "):
            node_colors.append(palette["paper"])
        elif label.startswith("📦 "):
            node_colors.append(palette["repo"])
        else:
            node_colors.append(palette["lab"])

    fig = go.Figure(
        go.Sankey(
            arrangement="snap",
            node=dict(
                label=nodes,
                pad=32,  # gap между узлами в колонке
                thickness=18,
                color=node_colors,
                line=dict(color="black", width=0.5),
            ),
            link=dict(
                source=src,
                target=dst,
                value=val,
                color=link_colors,
            ),
        )
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=20, b=20),
        height=700,  # выше — больше места под gaps
        font=dict(size=11),
    )
    return fig


# ----------------------------- ВКЛАДКА 3: СЕТЬ -----------------------------


def render_network(df_links: pd.DataFrame) -> None:
    st.caption(
        "Большой граф: все авторы и репо выбранных лаб в одном поле. "
        "Если один автор работал в нескольких — он будет соединять группы. "
        "Один репозиторий, цитируемый из разных публикаций, появляется один раз."
    )
    mode_label = st.radio(
        "Что ставим в центр графа",
        options=[
            "human-first (автор)",
            "paper-first (публикация)",
            "repo-first (репозиторий)",
        ],
        horizontal=True,
        key="network_mode",
        help=(
            "human-first — рёбра автор↔его публикации и автор↔его репо. "
            "paper-first — публикация в центре: paper↔author, paper↔repo. "
            "repo-first — репо в центре: repo↔его публикации и repo↔его авторы."
        ),
    )
    mode = mode_label.split("-")[0]
    c1, c2 = st.columns([1, 2])
    with c1:
        show_labels = st.toggle(
            "Показывать подписи", value=True, key="labels_network"
        )
    with c2:
        kinds = st.multiselect(
            "Показывать на графе",
            options=["публикации", "репо", "ИТМО-авторы", "внешние авторы"],
            default=["публикации", "репо", "ИТМО-авторы"],
            key="kinds_network",
            help="Снимешь галочку — узлы этого типа и их рёбра исчезают.",
        )
    show_papers = "публикации" in kinds
    show_repos = "репо" in kinds
    show_itmo = "ИТМО-авторы" in kinds
    show_ext = "внешние авторы" in kinds

    df_lab = df_links[df_links["lab"].notna()].copy()
    pub_ids = df_lab["publication_id"].unique().tolist()
    df_authors = load_authors_for(tuple(pub_ids))

    G = nx.Graph()
    for pid in pub_ids:
        lab = df_lab[df_lab["publication_id"] == pid]["lab"].iloc[0]
        G.add_node(("paper", pid), kind="paper", lab=lab, label=pid)
    for _, row in df_lab.iterrows():
        G.add_node(
            ("repo", row["url"]), kind="repo", lab=row["lab"], label=row["repo_name"]
        )
    for _, row in df_authors.iterrows():
        node_id = ("author", row["name"])
        if node_id not in G:
            G.add_node(
                node_id,
                kind="author",
                label=row["name"],
                person_type=row["person_type"],
            )

    # Карта url → is_relevant, чтобы статус репо переносился на любое
    # ребро, в которое этот репо входит.
    relevance_by_repo = {
        row["url"]: row.get("is_relevant") for _, row in df_lab.iterrows()
    }

    def repo_status(url: str):
        return relevance_by_repo.get(url)

    # --- рёбра зависят от режима ---
    if mode == "paper":
        # paper в центре: paper↔repo, paper↔author
        for _, row in df_lab.iterrows():
            G.add_edge(
                ("paper", row["publication_id"]),
                ("repo", row["url"]),
                is_relevant=row.get("is_relevant"),
            )
        for _, row in df_authors.iterrows():
            G.add_edge(("paper", row["publication_id"]), ("author", row["name"]))
    elif mode == "human":
        # автор в центре: автор↔его публикации, автор↔его репо (минуя paper)
        for _, row in df_authors.iterrows():
            G.add_edge(("paper", row["publication_id"]), ("author", row["name"]))
        for author_name in df_authors["name"].unique():
            his_pubs = df_authors[df_authors["name"] == author_name][
                "publication_id"
            ].tolist()
            his_repos = df_lab[df_lab["publication_id"].isin(his_pubs)]["url"].unique()
            for repo_url in his_repos:
                G.add_edge(
                    ("author", author_name),
                    ("repo", repo_url),
                    is_relevant=repo_status(repo_url),
                )
    else:  # mode == "repo"
        # репо в центре: репо↔его публикации, репо↔его авторы
        for _, row in df_lab.iterrows():
            G.add_edge(
                ("repo", row["url"]),
                ("paper", row["publication_id"]),
                is_relevant=row.get("is_relevant"),
            )
        for _, row in df_lab.iterrows():
            authors_of_pub = df_authors[
                df_authors["publication_id"] == row["publication_id"]
            ]["name"].tolist()
            for author_name in authors_of_pub:
                G.add_edge(
                    ("repo", row["url"]),
                    ("author", author_name),
                    is_relevant=row.get("is_relevant"),
                )

    # Применяем фильтр по типам узлов — связанные рёбра уйдут вместе с узлами.
    to_remove = []
    for node, data in G.nodes(data=True):
        kind = data["kind"]
        if kind == "paper" and not show_papers:
            to_remove.append(node)
        elif kind == "repo" and not show_repos:
            to_remove.append(node)
        elif kind == "author":
            if data.get("person_type") == "itmo" and not show_itmo:
                to_remove.append(node)
            elif data.get("person_type") != "itmo" and not show_ext:
                to_remove.append(node)
    G.remove_nodes_from(to_remove)

    if G.number_of_nodes() == 0:
        st.info("Нет данных для графа.")
        return

    # В human-first у автора рёбра идут и к публикациям, и к репо — узлов
    # становится плотнее, увеличиваем зазор k и число итераций.
    k_factor = {"human": 4.0, "repo": 2.5, "paper": 1.8}.get(mode, 1.5)
    pos = nx.spring_layout(
        G, seed=7, k=k_factor / (len(G.nodes) ** 0.5), iterations=300
    )

    traces = _build_edge_traces(G, pos)

    # Группируем репо: для каждой подсвечиваемой лабы — свой trace,
    # все остальные — один общий серый.
    repo_groups: dict[str, list] = {}
    for node, data in G.nodes(data=True):
        if data["kind"] != "repo":
            continue
        org = data.get("lab")
        bucket = org if org in LABS else "_other"
        repo_groups.setdefault(bucket, []).append((node, data))

    for bucket, nodes_in in repo_groups.items():
        xs, ys, texts = [], [], []
        for node, data in nodes_in:
            x, y = pos[node]
            xs.append(x)
            ys.append(y)
            texts.append(data["label"])
        short_labels = [t.split("/", 1)[1] if "/" in t else t for t in texts]
        if bucket == "_other":
            name = f"репо (другие, {len(nodes_in)} шт.)"
            color = NEUTRAL_LAB_COLOR
        else:
            name = f"репо {LABS[bucket]['title']}"
            color = LAB_COLORS[bucket]
        traces.append(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers+text" if show_labels else "markers",
                text=short_labels if show_labels else None,
                textposition="top center",
                textfont=dict(size=11, color="#f0f0f0"),
                hovertext=texts,
                hoverinfo="text",
                name=name,
                marker=dict(
                    symbol="diamond",
                    size=18,
                    color=color,
                    line=dict(color="black", width=1.5),
                ),
            )
        )

    paper_x, paper_y, paper_text = [], [], []
    for node, data in G.nodes(data=True):
        if data["kind"] == "paper":
            x, y = pos[node]
            paper_x.append(x)
            paper_y.append(y)
            paper_text.append(data["label"])
    if paper_x:
        traces.append(
            go.Scatter(
                x=paper_x,
                y=paper_y,
                mode="markers",
                text=paper_text,
                hovertext=paper_text,
                hoverinfo="text",
                name="публикация",
                marker=dict(
                    symbol="square",
                    size=12,
                    color="#bdbdbd",
                    line=dict(color="#525252", width=1),
                ),
            )
        )

    itmo_x, itmo_y, itmo_text = [], [], []
    ext_x, ext_y, ext_text = [], [], []
    for node, data in G.nodes(data=True):
        if data["kind"] != "author":
            continue
        x, y = pos[node]
        if data.get("person_type") == "itmo":
            itmo_x.append(x)
            itmo_y.append(y)
            itmo_text.append(data["label"])
        else:
            ext_x.append(x)
            ext_y.append(y)
            ext_text.append(data["label"])
    if itmo_x:
        traces.append(
            go.Scatter(
                x=itmo_x,
                y=itmo_y,
                mode="markers+text" if show_labels else "markers",
                text=itmo_text if show_labels else None,
                textposition="top center",
                textfont=dict(size=11, color="#f0f0f0"),
                hovertext=itmo_text,
                hoverinfo="text",
                name="автор ИТМО",
                marker=dict(
                    symbol="circle",
                    size=12,
                    color="#fee391",
                    line=dict(color="#cc4c02", width=1.5),
                ),
            )
        )
    if ext_x:
        traces.append(
            go.Scatter(
                x=ext_x,
                y=ext_y,
                mode="markers",
                text=ext_text,
                hovertext=ext_text,
                hoverinfo="text",
                name="внешний автор",
                marker=dict(
                    symbol="circle",
                    size=8,
                    color="#f0f0f0",
                    line=dict(color="#969696", width=1),
                ),
            )
        )

    fig = go.Figure(data=traces)
    fig.update_layout(
        showlegend=True,
        hovermode="closest",
        margin=dict(b=20, l=5, r=5, t=20),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        height=700,
    )
    st.plotly_chart(fig, width="stretch")


# ----------------------------- ВКЛАДКА 5: АВТОРЫ ---------------------------


def render_authors(df_links: pd.DataFrame) -> None:
    df_lab = df_links[df_links["lab"].notna()].copy()
    pub_ids = df_lab["publication_id"].unique().tolist()
    df_authors = load_authors_for(tuple(pub_ids))
    if df_authors.empty:
        st.info("Нет данных по авторам.")
        return

    # Каждый автор — сколько публикаций, в каких лабах
    rows: list[dict] = []
    for name, grp in df_authors.groupby("name"):
        pub_set = set(grp["publication_id"])
        labs_for_author = sorted(
            df_lab[df_lab["publication_id"].isin(pub_set)]["lab"].dropna().unique()
        )
        labs_display = ", ".join(
            (f"★ {LABS[la]['title']}" if la in LABS else la)
            for la in labs_for_author
        )
        rows.append(
            {
                "name": name,
                "person_type": grp["person_type"].iloc[0],
                "pubs": len(pub_set),
                "labs": labs_display,
            }
        )
    df_summary = (
        pd.DataFrame(rows)
        .sort_values(["pubs", "name"], ascending=[False, True])
        .reset_index(drop=True)
    )

    st.markdown("#### Карта авторов")
    st.caption(
        "Один автор может быть связан с несколькими публикациями и даже сразу с двумя "
        "лабами — это и есть «мосты» между группами."
    )
    df_display = df_summary.copy()
    df_display["тип"] = df_display["person_type"].map(
        {"itmo": "ИТМО", "external": "внешний"}
    )
    st.dataframe(
        df_display[["name", "тип", "pubs", "labs"]].rename(
            columns={"name": "автор", "pubs": "публикаций", "labs": "лаборатории"}
        ),
        width="stretch",
        hide_index=True,
    )

    st.markdown("#### Профиль автора")
    selected = st.selectbox(
        "Выбери автора, чтобы увидеть его публикации и репозитории",
        options=df_summary["name"].tolist(),
    )
    if not selected:
        return

    author_pubs = df_authors[df_authors["name"] == selected]["publication_id"].unique()
    df_author_links = df_lab[df_lab["publication_id"].isin(author_pubs)].copy()
    df_author_links["вердикт"] = (
        df_author_links["is_relevant"].map({1: "✓", 0: "✗"}).fillna("?")
    )

    col_l, col_r = st.columns([1, 1])
    col_l.metric("Публикаций в выборке", len(author_pubs))
    col_r.metric("Репозиториев", df_author_links["url"].nunique())

    pubs_for_table = (
        df_author_links[["publication_id", "title", "year", "lab"]]
        .drop_duplicates("publication_id")
        .rename(
            columns={
                "publication_id": "id",
                "title": "статья",
                "year": "год",
                "lab": "лаба",
            }
        )
    )
    st.markdown("**Публикации:**")
    st.dataframe(pubs_for_table, width="stretch", hide_index=True)

    repos_for_table = (
        df_author_links[["repo_name", "lab", "is_relevant"]]
        .drop_duplicates("repo_name")
        .rename(
            columns={
                "repo_name": "репо",
                "lab": "лаба",
                "is_relevant": "статус",
            }
        )
    )
    repos_for_table["статус"] = (
        repos_for_table["статус"]
        .map({1: "Авторский", 0: "Неподтверждённый"})
        .fillna("?")
    )
    st.markdown("**Репозитории:**")
    st.dataframe(repos_for_table, width="stretch", hide_index=True)


# ----------------------------- MAIN ----------------------------------------


def main() -> None:
    st.set_page_config(page_title="PAUK - лаборатории ИТМО", layout="wide")
    st.title("PAUK")
    st.caption("Демо-визуализация по нескольким лабам.")

    df_links = load_links()
    df_in_labs = df_links[df_links["lab"].notna()]
    if df_in_labs.empty:
        st.error("В repo_links нет данных с GitHub-ссылками.")
        return

    # Все организации из БД, отсортированные по числу публикаций.
    # Известные (из LABS) маркируем «звёздочкой» в выпадающем списке.
    org_ranking = (
        df_in_labs.groupby("lab")["publication_id"]
        .nunique()
        .sort_values(ascending=False)
    )

    def _format_org(org: str) -> str:
        n = int(org_ranking.get(org, 0))
        marker = "★ " if org in LABS else ""
        return f"{marker}{org}  ({n} публ.)"

    tab1, tab2, tab3, tab4 = st.tabs(["Лаборатория", "Сравнение", "Сеть", "Авторы"])
    with tab1:
        lab_key = st.selectbox(
            "Лаборатория (GitHub-организация)",
            options=org_ranking.index.tolist(),
            format_func=_format_org,
            help="★ — лаборатории с заранее заданным цветом. Остальные показаны "
            "тем же серым цветом, что и в общих графах.",
        )
        render_lab_card(lab_key, df_links)
    with tab2:
        render_comparison(df_links)
    with tab3:
        render_network(df_links)
    with tab4:
        render_authors(df_links)


if __name__ == "__main__":
    main()
