"""Интерактивное демо PAUK по департаментам ИТМО.

Работает поверх БД из new/OPENSOURCE_departaments_bkp.db.

Группировка публикаций — через persons_itmo.department (ID департаментов
через '; '), которые сопоставлены LLM-скриптом `new/4_department_enrich.py`.

Запуск из корня проекта:
    uv run streamlit run app_departments.py
"""

import sqlite3
from pathlib import Path

import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "data" / "OPENSOURCE_departaments_bkp.db"
OLD_DB_PATH = ROOT_DIR / "data" / "itmo_research_opensource.db"


LAB_COLORS_BY_NAME: dict[str, str] = {
    "School of Physics and Engineering": "#3182bd",
    "Faculty of Control Systems and Robotics": "#e6550d",
    "Infochemistry Scientific Center": "#74c476",
    "Faculty of Biotechnologies (BioTech)": "#fdae6b",
    "International Research and Educational Center for Physics of Nanostructures": "#9467bd",
}


STATUS_LABELS = {
    "confirmed": "Есть авторский репо",
    "rejected": "Все кандидаты отклонены",
    "none": "Репо не найден",
}
STATUS_COLORS = {
    "confirmed": "#31a354",
    "rejected": "#de2d26",
    "none": "#9e9e9e",
}


# ----------------------------- ЗАГРУЗКА ДАННЫХ -----------------------------


@st.cache_data
def load_publications() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT id, title, journal, year, publication_date, doi, openalex_url
        FROM publications
        """,
        conn,
    )
    conn.close()
    return df


@st.cache_data
def load_departments() -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query("SELECT id, name_en, name_ru FROM departments", conn)
    conn.close()
    return df


@st.cache_data
def load_persons_itmo() -> pd.DataFrame:
    """ITMO-персоны с заполненным полем department."""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT id, name_en, department
        FROM persons_itmo
        WHERE department IS NOT NULL AND department != ''
        """,
        conn,
    )
    conn.close()
    return df


@st.cache_data
def load_authorships() -> pd.DataFrame:
    """Все строки publication_authors с подтянутым именем."""
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """
        SELECT pa.publication_id, pa.person_id, pa.person_type, pa.author_position,
               COALESCE(pi.name_en, pe.name_en, 'Unknown') AS name
        FROM publication_authors pa
        LEFT JOIN persons_itmo     pi ON pi.id = pa.person_id AND pa.person_type='itmo'
        LEFT JOIN persons_external pe ON pe.id = pa.person_id AND pa.person_type='external'
        """,
        conn,
    )
    conn.close()
    return df


@st.cache_data
def load_pub_dept_mapping() -> pd.DataFrame:
    """Возвращает df: (publication_id, department_id, department_name).

    Одна публикация присутствует столько раз, сколько у её ITMO-авторов
    разных департаментов (один автор тоже может быть в нескольких).
    Это и есть «мостовые» связи между подразделениями.
    """
    df_depts = load_departments()
    df_persons = load_persons_itmo()
    df_auths = load_authorships()

    name_map = dict(zip(df_depts["id"], df_depts["name_en"]))

    person_to_depts: dict[str, list[str]] = {}
    for _, row in df_persons.iterrows():
        ids = [d.strip() for d in str(row["department"]).split(";") if d.strip()]
        person_to_depts[row["id"]] = ids

    rows = []
    df_itmo_auth = df_auths[df_auths["person_type"] == "itmo"]
    for _, row in df_itmo_auth.iterrows():
        for dept_id in person_to_depts.get(row["person_id"], []):
            if dept_id in name_map:
                rows.append(
                    {
                        "publication_id": row["publication_id"],
                        "person_id": row["person_id"],
                        "person_name": row["name"],
                        "department_id": dept_id,
                        "department_name": name_map[dept_id],
                    }
                )
    return pd.DataFrame(rows).drop_duplicates()


def color_for(dept_name: str) -> str:
    return LAB_COLORS_BY_NAME.get(dept_name, "#666666")


@st.cache_data
def load_pub_status() -> dict[str, str]:
    """Возвращает {publication_id: 'confirmed'|'rejected'} через ATTACH старой БД.
    Публикации без записи в repo_links считаются 'none' (не возвращаются)."""
    if not OLD_DB_PATH.exists():
        return {}
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(f"ATTACH DATABASE '{OLD_DB_PATH}' AS oldb")
        df = pd.read_sql_query(
            """
            SELECT publication_id, MAX(is_relevant) AS best, COUNT(*) AS n
            FROM oldb.repo_links
            GROUP BY publication_id
            """,
            conn,
        )
    except Exception:
        conn.close()
        return {}
    conn.close()
    result: dict[str, str] = {}
    for _, row in df.iterrows():
        if row["best"] == 1:
            result[row["publication_id"]] = "confirmed"
        elif row["n"] > 0:
            result[row["publication_id"]] = "rejected"
    return result


def render_status_filter(key: str) -> set[str]:
    selected = st.multiselect(
        "Статус публикации",
        options=list(STATUS_LABELS.keys()),
        default=list(STATUS_LABELS.keys()),
        format_func=lambda k: STATUS_LABELS[k],
        key=f"status_{key}",
        help="Зелёный — авторский репо найден; красный — кандидаты есть, но все отклонены LLM; серый — репо не нашли.",
    )
    return set(selected) if selected else set(STATUS_LABELS.keys())


# ----------------------------- ВКЛАДКА 1: ДЕПАРТАМЕНТ ----------------------


def render_dept_card(dept_id: str, dept_name: str, df_map: pd.DataFrame) -> None:
    df_dept = df_map[df_map["department_id"] == dept_id]
    df_pubs = load_publications()
    df_auths = load_authorships()

    pub_ids = df_dept["publication_id"].unique().tolist()
    itmo_authors = df_dept["person_name"].nunique()
    df_pub_auths = df_auths[df_auths["publication_id"].isin(pub_ids)]
    ext_authors = df_pub_auths[df_pub_auths["person_type"] == "external"][
        "name"
    ].nunique()

    # Связанные департаменты — те, кто делит публикации с этим
    related = df_map[
        df_map["publication_id"].isin(pub_ids) & (df_map["department_id"] != dept_id)
    ]
    related_count = related["department_id"].nunique()

    st.subheader(dept_name)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Публикаций", len(pub_ids))
    c2.metric("ИТМО-авторов", itmo_authors)
    c3.metric("Внешних соавторов", ext_authors)
    c4.metric(
        "Связанных департаментов",
        related_count,
        help="С которыми делятся хотя бы одной публикацией через общих ITMO-авторов.",
    )

    if not pub_ids:
        st.info("Нет публикаций.")
        return

    st.markdown("#### Топ публикаций по числу ITMO-авторов из этого департамента")
    top_pubs = (
        df_dept.groupby("publication_id").size().sort_values(ascending=False).head(20)
    )
    df_top = (
        df_pubs[df_pubs["id"].isin(top_pubs.index)]
        .copy()
        .merge(
            top_pubs.rename("itmo_авторов").reset_index(),
            left_on="id",
            right_on="publication_id",
        )
    )
    df_top = df_top[["id", "title", "year", "journal", "itmo_авторов"]].sort_values(
        "itmo_авторов", ascending=False
    )
    df_top.columns = ["id", "статья", "год", "журнал", "авторов из департамента"]
    st.dataframe(df_top, width="stretch", hide_index=True)

    st.markdown("#### Граф: авторы департамента ↔ их публикации")
    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        show_labels = st.toggle("Подписи на графе", value=True, key=f"labels_{dept_id}")
    with c2:
        allowed_statuses = render_status_filter(f"dept_{dept_id}")
    with c3:
        kinds = st.multiselect(
            "Показывать на графе",
            options=["публикации", "ИТМО-авторы", "внешние авторы"],
            default=["публикации", "ИТМО-авторы"],
            key=f"kinds_{dept_id}",
        )
    pub_statuses = load_pub_status()
    fig = build_dept_graph(
        dept_id,
        dept_name,
        df_map,
        df_auths,
        kinds=kinds,
        show_labels=show_labels,
        pub_statuses=pub_statuses,
        allowed_statuses=allowed_statuses,
    )
    st.plotly_chart(fig, width="stretch")


def build_dept_graph(
    dept_id: str,
    dept_name: str,
    df_map: pd.DataFrame,
    df_auths: pd.DataFrame,
    kinds: list[str],
    show_labels: bool,
    pub_statuses: dict[str, str] | None = None,
    allowed_statuses: set[str] | None = None,
) -> go.Figure:
    G = nx.Graph()
    color = color_for(dept_name)
    pub_statuses = pub_statuses or {}
    allowed_statuses = allowed_statuses or set(STATUS_LABELS.keys())

    df_dept = df_map[df_map["department_id"] == dept_id]
    pub_ids = [
        pid
        for pid in df_dept["publication_id"].unique().tolist()
        if pub_statuses.get(pid, "none") in allowed_statuses
    ]
    dept_persons = df_dept["person_id"].unique().tolist()

    show_papers = "публикации" in kinds
    show_itmo = "ИТМО-авторы" in kinds
    show_ext = "внешние авторы" in kinds

    if show_papers:
        for pid in pub_ids:
            G.add_node(("paper", pid), kind="paper", label=pid)

    df_pub_auths = df_auths[df_auths["publication_id"].isin(pub_ids)]
    for _, row in df_pub_auths.iterrows():
        if row["person_type"] == "itmo" and not show_itmo:
            continue
        if row["person_type"] == "external" and not show_ext:
            continue
        # Для ИТМО: только те, кто связан с нашим dept_id
        if row["person_type"] == "itmo" and row["person_id"] not in dept_persons:
            continue
        G.add_node(
            ("author", row["name"]),
            kind="author",
            label=row["name"],
            person_type=row["person_type"],
        )
        if show_papers:
            G.add_edge(("paper", row["publication_id"]), ("author", row["name"]))

    if G.number_of_nodes() == 0:
        return go.Figure()

    pos = nx.spring_layout(G, seed=7, k=2.0 / (len(G.nodes) ** 0.5), iterations=300)

    traces = _edge_trace(G, pos)
    traces += _node_traces(G, pos, color, show_labels, pub_statuses=pub_statuses)
    fig = go.Figure(data=traces)
    fig.update_layout(
        showlegend=True,
        hovermode="closest",
        margin=dict(b=20, l=5, r=5, t=20),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        height=600,
    )
    return fig


def _edge_trace(G: nx.Graph, pos: dict) -> list[go.Scatter]:
    edge_x, edge_y = [], []
    for u, v in G.edges():
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]
    return [
        go.Scatter(
            x=edge_x,
            y=edge_y,
            line=dict(width=0.4, color="#9e9e9e"),
            hoverinfo="none",
            mode="lines",
            showlegend=False,
        )
    ]


def _paper_traces_by_status(
    G: nx.Graph, pos: dict, pub_statuses: dict[str, str], show_labels: bool
) -> list[go.Scatter]:
    """Три отдельных trace для публикаций — по статусу (зелёный/красный/серый)."""
    groups: dict[str, list[tuple]] = {"confirmed": [], "rejected": [], "none": []}
    for node, data in G.nodes(data=True):
        if data["kind"] != "paper":
            continue
        status = pub_statuses.get(node[1], "none")
        groups[status].append((node, data))
    traces: list[go.Scatter] = []
    for status, items in groups.items():
        if not items:
            continue
        xs, ys, texts = [], [], []
        for node, data in items:
            x, y = pos[node]
            xs.append(x)
            ys.append(y)
            texts.append(data["label"])
        traces.append(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers+text" if show_labels else "markers",
                text=texts if show_labels else None,
                textposition="top center",
                textfont=dict(size=9, color="#f0f0f0"),
                hovertext=texts,
                hoverinfo="text",
                name=f"публикация: {STATUS_LABELS[status]}",
                marker=dict(
                    symbol="square",
                    size=11,
                    color=STATUS_COLORS[status],
                    line=dict(color="black", width=0.5),
                ),
            )
        )
    return traces


def _node_traces(
    G: nx.Graph,
    pos: dict,
    dept_color: str,
    show_labels: bool,
    pub_statuses: dict[str, str] | None = None,
) -> list[go.Scatter]:
    pub_statuses = pub_statuses or {}
    # publications — отдельным набором trace по статусу (3 цвета)
    traces = _paper_traces_by_status(G, pos, pub_statuses, show_labels)
    # авторы — как раньше
    author_styles = {
        "author_itmo": dict(
            symbol="circle",
            size=12,
            color=dept_color,
            border="black",
            name="автор ИТМО",
        ),
        "author_external": dict(
            symbol="circle",
            size=8,
            color="#f0f0f0",
            border="#969696",
            name="внешний автор",
        ),
    }
    visible_labels = {"author_itmo"} if show_labels else set()
    for style_key, style in author_styles.items():
        xs, ys, texts = [], [], []
        for node, data in G.nodes(data=True):
            if data["kind"] != "author":
                continue
            k = (
                "author_itmo"
                if data.get("person_type") == "itmo"
                else "author_external"
            )
            if k != style_key:
                continue
            x, y = pos[node]
            xs.append(x)
            ys.append(y)
            texts.append(data["label"])
        if not xs:
            continue
        show_text = style_key in visible_labels
        traces.append(
            go.Scatter(
                x=xs,
                y=ys,
                mode="markers+text" if show_text else "markers",
                text=texts if show_text else None,
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
    return traces


# ----------------------------- ВКЛАДКА 2: СРАВНЕНИЕ ------------------------


def render_comparison(df_map: pd.DataFrame) -> None:
    summary = (
        df_map.groupby(["department_id", "department_name"])
        .agg(
            публикаций=("publication_id", "nunique"),
            авторов=("person_id", "nunique"),
        )
        .reset_index()
        .sort_values("публикаций", ascending=False)
        .head(15)
    )
    summary["★"] = summary["department_name"].map(
        lambda n: "★" if n in LAB_COLORS_BY_NAME else ""
    )
    st.markdown("#### Топ-15 департаментов по числу публикаций")
    st.dataframe(
        summary[["★", "department_name", "публикаций", "авторов"]].rename(
            columns={"department_name": "департамент"}
        ),
        width="stretch",
        hide_index=True,
    )

    st.markdown("#### Sankey: потоки между департаментом, авторами и публикациями")

    # Топ-30 департаментов для выпадающего списка.
    top_dept_ranking = (
        df_map.groupby(["department_id", "department_name"])["publication_id"]
        .nunique()
        .sort_values(ascending=False)
        .head(30)
    )
    options = [idx[0] for idx in top_dept_ranking.index.tolist()]
    counts = {idx[0]: int(n) for idx, n in top_dept_ranking.items()}
    id_to_name = dict(zip(df_map["department_id"], df_map["department_name"]))

    def _fmt(d: str) -> str:
        name = id_to_name.get(d, d)
        marker = "★ " if name in LAB_COLORS_BY_NAME else ""
        return f"{marker}{name}  ({counts.get(d, 0)} публ.)"

    # Дефолт — самый крупный подсвеченный, иначе первый в топе.
    default_dept = next(
        (d for d in options if id_to_name.get(d) in LAB_COLORS_BY_NAME), options[0]
    )
    selected_dept = st.selectbox(
        "Департамент в Sankey",
        options=options,
        index=options.index(default_dept),
        format_func=_fmt,
    )

    mode_label = st.radio(
        "Что ставим в центр потока",
        options=[
            "human-first (автор)",
            "paper-first (публикация)",
            "department-first (департамент)",
        ],
        horizontal=True,
        help=(
            "human-first — депт→автор→публикация. "
            "paper-first — депт→публикация→автор. "
            "department-first — автор→депт→публикация (видно «мостовых» авторов "
            "с двумя и более департаментами)."
        ),
    )
    mode = mode_label.split("-")[0]

    df_focus = df_map[df_map["department_id"] == selected_dept]
    fig = build_sankey(df_focus, mode=mode)
    st.plotly_chart(fig, width="stretch")


def build_sankey(df_map: pd.DataFrame, mode: str) -> go.Figure:
    nodes: list[str] = []
    node_index: dict[str, int] = {}
    src, dst, val, link_colors = [], [], [], []

    def add(label: str) -> int:
        if label not in node_index:
            node_index[label] = len(nodes)
            nodes.append(label)
        return node_index[label]

    NEUTRAL = "rgba(180,180,180,0.4)"

    # Фильтрация по департаменту уже выполнена в render_comparison.
    df_lab = df_map

    # ITMO-авторы, привязанные к публикациям + департаментам
    for _, row in df_lab.iterrows():
        dept = f"🏛 {row['department_name']}"
        author = f"👤 {row['person_name']}"
        pub = f"📄 {row['publication_id']}"

        if mode == "human":
            # депт → автор → публикация
            src.append(add(dept))
            dst.append(add(author))
            val.append(1)
            link_colors.append(NEUTRAL)
            src.append(add(author))
            dst.append(add(pub))
            val.append(1)
            link_colors.append(NEUTRAL)
        elif mode == "paper":
            # депт → публикация → автор
            src.append(add(dept))
            dst.append(add(pub))
            val.append(1)
            link_colors.append(NEUTRAL)
            src.append(add(pub))
            dst.append(add(author))
            val.append(1)
            link_colors.append(NEUTRAL)
        else:  # department
            # автор → депт → публикация (для cross-department «мостов»)
            src.append(add(author))
            dst.append(add(dept))
            val.append(1)
            link_colors.append(NEUTRAL)
            src.append(add(dept))
            dst.append(add(pub))
            val.append(1)
            link_colors.append(NEUTRAL)

    palette = {"dept": "#3182bd", "author": "#fdae6b", "paper": "#bdbdbd"}
    node_colors = []
    for label in nodes:
        if label.startswith("👤 "):
            node_colors.append(palette["author"])
        elif label.startswith("📄 "):
            node_colors.append(palette["paper"])
        else:
            node_colors.append(palette["dept"])

    fig = go.Figure(
        go.Sankey(
            arrangement="snap",
            node=dict(
                label=nodes,
                pad=24,
                thickness=16,
                color=node_colors,
                line=dict(color="black", width=0.5),
            ),
            link=dict(source=src, target=dst, value=val, color=link_colors),
        )
    )
    fig.update_layout(
        margin=dict(l=10, r=10, t=20, b=20), height=700, font=dict(size=11)
    )
    return fig


# ----------------------------- ВКЛАДКА 3: СЕТЬ -----------------------------


def render_network(df_map: pd.DataFrame) -> None:
    st.caption(
        "Большой граф: авторы и публикации выбранных департаментов. "
        "Автор, относящийся к нескольким департаментам, становится «мостом» между ними."
    )

    top_dept_ids = (
        df_map.groupby("department_id")["publication_id"]
        .nunique()
        .sort_values(ascending=False)
        .head(20)
        .index.tolist()
    )
    id_to_name = dict(zip(df_map["department_id"], df_map["department_name"]))

    default_depts = [
        d for d in top_dept_ids if id_to_name.get(d) in LAB_COLORS_BY_NAME
    ][:5] or top_dept_ids[:5]

    selected_depts = st.multiselect(
        "Департаменты",
        options=top_dept_ids,
        default=default_depts,
        format_func=lambda d: id_to_name.get(d, d),
        help="По умолчанию подсвеченные ИТМО-департаменты. Можно выбрать любые из топ-20.",
    )

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        show_labels = st.toggle("Подписи на графе", value=True, key="labels_net")
    with c2:
        allowed_statuses = render_status_filter("net")
    with c3:
        kinds = st.multiselect(
            "Показывать на графе",
            options=["публикации", "ИТМО-авторы", "внешние авторы"],
            default=["публикации", "ИТМО-авторы"],
            key="kinds_net",
        )

    if not selected_depts:
        st.info("Выбери хотя бы один департамент.")
        return

    pub_statuses = load_pub_status()
    df_focus = df_map[df_map["department_id"].isin(selected_depts)]
    df_auths = load_authorships()

    pub_ids = [
        pid
        for pid in df_focus["publication_id"].unique().tolist()
        if pub_statuses.get(pid, "none") in allowed_statuses
    ]
    if not pub_ids:
        st.info("После фильтров публикаций не осталось.")
        return

    G = nx.Graph()
    show_papers = "публикации" in kinds
    show_itmo = "ИТМО-авторы" in kinds
    show_ext = "внешние авторы" in kinds

    if show_papers:
        for pid in pub_ids:
            G.add_node(("paper", pid), kind="paper", label=pid)

    # для каждого ITMO-автора собираем его список депов (из focus)
    person_depts = {}
    for _, row in df_focus.iterrows():
        person_depts.setdefault(row["person_id"], []).append(row["department_id"])

    df_pa = df_auths[df_auths["publication_id"].isin(pub_ids)]
    for _, row in df_pa.iterrows():
        if row["person_type"] == "itmo":
            if not show_itmo:
                continue
            if row["person_id"] not in person_depts:
                continue
            # цвет автора по первому из его подсвеченных департаментов
            depts = person_depts[row["person_id"]]
            colored_dept = next(
                (d for d in depts if id_to_name.get(d) in LAB_COLORS_BY_NAME),
                depts[0],
            )
            G.add_node(
                ("author", row["name"]),
                kind="author",
                person_type="itmo",
                dept_id=colored_dept,
                label=row["name"],
            )
        else:
            if not show_ext:
                continue
            G.add_node(
                ("author", row["name"]),
                kind="author",
                person_type="external",
                label=row["name"],
            )
        if show_papers:
            G.add_edge(("paper", row["publication_id"]), ("author", row["name"]))

    if G.number_of_nodes() == 0:
        st.info("После фильтров не осталось узлов.")
        return

    pos = nx.spring_layout(G, seed=7, k=2.0 / (len(G.nodes) ** 0.5), iterations=300)

    traces = _edge_trace(G, pos)
    # публикации — окрашиваем по статусу (зелёный/красный/серый)
    traces += _paper_traces_by_status(G, pos, pub_statuses, show_labels)

    # itmo-авторы — по департаментам (подсвеченные)
    for dept_id in selected_depts:
        dept_name = id_to_name.get(dept_id, dept_id)
        color = color_for(dept_name)
        xs, ys, texts = [], [], []
        for node, data in G.nodes(data=True):
            if (
                data["kind"] == "author"
                and data.get("person_type") == "itmo"
                and data.get("dept_id") == dept_id
            ):
                x, y = pos[node]
                xs.append(x)
                ys.append(y)
                texts.append(data["label"])
        if xs:
            traces.append(
                go.Scatter(
                    x=xs,
                    y=ys,
                    mode="markers+text" if show_labels else "markers",
                    text=texts if show_labels else None,
                    textposition="top center",
                    textfont=dict(size=11, color="#f0f0f0"),
                    hovertext=texts,
                    hoverinfo="text",
                    name=f"автор {dept_name}",
                    marker=dict(
                        symbol="circle",
                        size=12,
                        color=color,
                        line=dict(color="black", width=1.5),
                    ),
                )
            )

    # внешние авторы
    ext_x, ext_y, ext_text = [], [], []
    for node, data in G.nodes(data=True):
        if data["kind"] == "author" and data.get("person_type") == "external":
            x, y = pos[node]
            ext_x.append(x)
            ext_y.append(y)
            ext_text.append(data["label"])
    if ext_x:
        traces.append(
            go.Scatter(
                x=ext_x,
                y=ext_y,
                mode="markers",
                hovertext=ext_text,
                hoverinfo="text",
                name="внешний автор",
                marker=dict(
                    symbol="circle",
                    size=7,
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
        height=750,
    )
    st.plotly_chart(fig, width="stretch")


# ----------------------------- ВКЛАДКА 4: АВТОРЫ ---------------------------


def render_authors(df_map: pd.DataFrame) -> None:
    df_pubs = load_publications()
    df_auths = load_authorships()

    # каждая ITMO-персона: число публикаций + список её департаментов
    by_person = (
        df_map.groupby(["person_id", "person_name"])
        .agg(
            pubs=("publication_id", "nunique"),
            depts=("department_name", lambda s: ", ".join(sorted(set(s)))),
        )
        .reset_index()
        .sort_values("pubs", ascending=False)
    )

    st.markdown("#### Карта ИТМО-авторов")
    st.caption(
        "Один автор может быть связан с несколькими департаментами и публикациями — "
        "это «мостовые» исследователи."
    )
    st.dataframe(
        by_person.rename(
            columns={
                "person_name": "автор",
                "pubs": "публикаций",
                "depts": "департаменты",
            }
        )[["автор", "публикаций", "департаменты"]],
        width="stretch",
        hide_index=True,
    )

    st.markdown("#### Профиль автора")
    selected = st.selectbox(
        "Выбери автора",
        options=by_person["person_name"].tolist(),
    )
    if not selected:
        return

    df_person = df_map[df_map["person_name"] == selected]
    pub_ids = df_person["publication_id"].unique().tolist()
    dept_names = sorted(df_person["department_name"].unique())

    c1, c2 = st.columns(2)
    c1.metric("Публикаций", len(pub_ids))
    c2.metric("Департаментов", len(dept_names))

    st.markdown("**Департаменты:** " + ", ".join(dept_names))

    df_pa = df_auths[df_auths["publication_id"].isin(pub_ids)]
    co_authors = (
        df_pa[(df_pa["name"] != selected)]
        .groupby("name")
        .size()
        .sort_values(ascending=False)
    )

    st.markdown("**Топ соавторов:**")
    st.dataframe(
        co_authors.reset_index().rename(
            columns={"name": "соавтор", 0: "совместных публикаций"}
        ),
        width="stretch",
        hide_index=True,
    )

    st.markdown("**Публикации:**")
    df_show = df_pubs[df_pubs["id"].isin(pub_ids)].copy()
    st.dataframe(
        df_show[["id", "title", "year", "journal"]].rename(
            columns={"title": "статья", "year": "год", "journal": "журнал"}
        ),
        width="stretch",
        hide_index=True,
    )


# ----------------------------- MAIN ---------------------------------------


def main() -> None:
    st.set_page_config(page_title="PAUK — департаменты ИТМО", layout="wide")
    st.title("PAUK")
    st.caption(
        "Принадлежность публикации к департаменту выводится через её ИТМО-авторов."
    )

    df_map = load_pub_dept_mapping()
    if df_map.empty:
        st.error("Нет данных по департаментам в БД.")
        return

    dept_ranking = (
        df_map.groupby(["department_id", "department_name"])["publication_id"]
        .nunique()
        .sort_values(ascending=False)
    )
    id_to_name = dict(zip(df_map["department_id"], df_map["department_name"]))

    def _format(dept_id: str) -> str:
        name = id_to_name.get(dept_id, dept_id)
        n = int(dept_ranking.get((dept_id, name), 0))
        marker = "★ " if name in LAB_COLORS_BY_NAME else ""
        return f"{marker}{name}  ({n} публ.)"

    tab1, tab2, tab3, tab4 = st.tabs(["Департамент", "Сравнение", "Сеть", "Авторы"])
    with tab1:
        dept_id = st.selectbox(
            "Департамент",
            options=[idx[0] for idx in dept_ranking.index.tolist()],
            format_func=_format,
        )
        render_dept_card(dept_id, id_to_name.get(dept_id, dept_id), df_map)
    with tab2:
        render_comparison(df_map)
    with tab3:
        render_network(df_map)
    with tab4:
        render_authors(df_map)


if __name__ == "__main__":
    main()
