"""Moderation screen for agent-submitted link evidence (`/suggestions`).

CoScientist submissions live in MongoDB (`link_suggestions`) and never mutate
Neo4j directly. An editor inspects each suggestion against live graph state and
explicitly approves (writing to Neo4j + `graph_overrides`) or rejects it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from hashlib import sha256
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from neo4j.exceptions import AuthError, DriverError, Neo4jError, ServiceUnavailable
from pymongo.errors import PyMongoError

from pauk.admin.deps import (
    MONGO_SILENT,
    CsrfChecked,
    CurrentUser,
    Db,
    Editor,
    Graph,
    MaybeGraph,
    Session,
    StoresReady,
    templates,
)
from pauk.admin.nodes import _record
from pauk.graph.mutations import (
    MutationError,
    NotFound,
    VersionConflict,
    create_node,
    create_relationship,
    delete_node,
    delete_relationship,
    find_repositories_by_url,
    folded_into,
    read_node,
    read_relationship,
    update_node,
)
from pauk.graph.overrides import (
    COLLECTION as OVERRIDES,
)
from pauk.graph.overrides import (
    CREATE,
    DELETE,
    LINK,
    SET,
    deactivate_override,
    override_generation,
    override_id,
    record_override,
    record_relationship_override,
    relationship_override_id,
)
from pauk.mcp.contracts import normalize_repo_url_query, safe_evidence_url, safe_url
from pauk.storage import link_suggestions

logger = logging.getLogger("pauk.admin")

router = APIRouter()

PAGE = 50
TABS = (link_suggestions.PENDING, link_suggestions.APPROVED, link_suggestions.REJECTED)

RELATION_LABELS = {
    link_suggestions.IMPLEMENTS: "авторский код (IMPLEMENTS)",
    link_suggestions.NOT_IMPLEMENTS: "не авторский код (снять IMPLEMENTS)",
    link_suggestions.MENTIONS: "упоминание кода (MENTIONS_LINK)",
    link_suggestions.FULL_TEXT: "ссылка на полный текст (pdf_urls)",
}


def _tab(value: object, default: str = link_suggestions.PENDING) -> str:
    """Restrict tab parameters to known redirect targets."""
    return value if value in TABS else default


def _redirect(tab: str, *, problem: str = "", done: str = "") -> RedirectResponse:
    query = f"?tab={tab}"
    if problem:
        query += f"&problem={quote(problem)}"
    if done:
        query += f"&done={done}"
    return RedirectResponse(f"/suggestions{query}", status_code=status.HTTP_303_SEE_OTHER)


def _node_href(label: str, node_id: str, back: str) -> str:
    return f"/nodes/{label}/{quote(node_id, safe='/')}?back={quote(back, safe='')}"


def _new_repository_id(graph, normalized_url: str) -> tuple[str, str | None]:
    """Derive a deterministic Repository id and short display name from a normalized URL."""
    parsed = urlsplit(normalized_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    name = segments[-1] if segments else None
    if parsed.hostname == "github.com" and len(segments) == 2:
        candidate = f"github_{segments[0].lower()}_{segments[1].lower()}"
        if graph.fetch_node_properties("Repository", candidate) is None:
            return candidate, name
    return f"repo_{sha256(normalized_url.encode('utf-8')).hexdigest()[:16]}", name


def _resolve_publication(graph, publication_id: str) -> tuple[str, dict[str, Any]] | None:
    try:
        return publication_id, read_node(graph, "Publication", publication_id)
    except NotFound:
        survivor = folded_into(graph, "Publication", publication_id)
        if survivor:
            try:
                return survivor, read_node(graph, "Publication", survivor)
            except NotFound:
                return None
        return None


def _resolve_repository(
    graph,
    repository_id: str | None,
    repository_url: str | None,
) -> tuple[str | None, dict[str, Any] | None, bool]:
    """Resolve canonical repository node by id/alias first, then unique normalized URL."""
    if repository_id:
        try:
            return repository_id, read_node(graph, "Repository", repository_id), False
        except NotFound:
            survivor = folded_into(graph, "Repository", repository_id)
            if survivor:
                try:
                    return survivor, read_node(graph, "Repository", survivor), False
                except NotFound:
                    pass
    if repository_url:
        matches = find_repositories_by_url(graph, repository_url)
        if len(matches) == 1:
            matched = matches[0]
            return str(matched["id"]), matched, False
        if len(matches) > 1:
            return None, None, True
    return None, None, False


def _edge_exists(
    graph,
    src_label: str,
    rel_type: str,
    tgt_label: str,
    src_id: str,
    tgt_id: str,
) -> dict[str, Any] | None:
    try:
        return read_relationship(graph, src_label, rel_type, tgt_label, src_id, tgt_id)
    except NotFound:
        return None


def _safe_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _safe_datetime(value: object) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _safe_prior(prior: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(prior, dict):
        return None
    return {
        **prior,
        "status": prior.get("status") if prior.get("status") in (link_suggestions.APPROVED, link_suggestions.REJECTED) else None,
        "reviewed_by": _safe_str(prior.get("reviewed_by")) or "?",
        "reviewed_at": _safe_datetime(prior.get("reviewed_at")),
    }


def _describe_row(
    row: dict[str, Any],
    *,
    prior: dict[str, Any] | None,
    graph,
    back: str,
) -> dict[str, Any]:
    """Attach safe display links and current graph state for one moderation card."""
    pub_id = _safe_str(row.get("publication_id")) or ""
    repo_id = _safe_str(row.get("repository_id"))
    relation = _safe_str(row.get("proposed_relation")) or ""
    raw_repo_url = _safe_str(row.get("repository_url"))
    safe_repo_url = safe_url(raw_repo_url)
    raw_evidence_url = _safe_str(row.get("evidence_url"))
    safe_ev_url = safe_evidence_url(raw_evidence_url)
    raw_count = row.get("submission_count")
    submission_count = raw_count if type(raw_count) is int and raw_count >= 1 else 1

    pub_title = None
    pub_doi = None
    pub_year = None
    pub_href = _node_href("Publication", pub_id, back) if pub_id else "#"
    pub_missing = not pub_id
    resolved_pub_id = pub_id or "?"

    resolved_repo_id = repo_id
    repo_href = _node_href("Repository", repo_id, back) if repo_id else None
    repo_name = None
    repo_graph_url = None
    repo_ambiguous = False
    implements_exists = False
    graph_up = graph is not None

    if graph is not None:
        if pub_id:
            pub = _resolve_publication(graph, pub_id)
            if pub is None:
                pub_missing = True
            else:
                resolved_pub_id, pub_props = pub
                pub_href = _node_href("Publication", resolved_pub_id, back)
                pub_title = pub_props.get("title")
                pub_doi = pub_props.get("doi")
                pub_year = pub_props.get("year")
        if relation != link_suggestions.FULL_TEXT:
            matched_id, repo_props, repo_ambiguous = _resolve_repository(graph, repo_id, raw_repo_url)
            if matched_id and repo_props is not None:
                resolved_repo_id = matched_id
                repo_href = _node_href("Repository", matched_id, back)
                repo_name = repo_props.get("name")
                repo_graph_url = safe_url(repo_props.get("url"))
                if not pub_missing:
                    implements_exists = (
                        _edge_exists(
                            graph, "Repository", "IMPLEMENTS", "Publication", matched_id, resolved_pub_id
                        )
                        is not None
                    )
            else:
                resolved_repo_id = None
                repo_href = None

    return {
        **row,
        "id": _safe_str(row.get("_id")) or "",
        "proposed_relation": relation,
        "relation_label": RELATION_LABELS.get(relation, relation or "неизвестно"),
        "rationale": _safe_str(row.get("rationale")) or "",
        "quote": _safe_str(row.get("quote")),
        "submitted_by": _safe_str(row.get("submitted_by")) or "?",
        "submitted_at": _safe_datetime(row.get("submitted_at")),
        "reviewed_at": _safe_datetime(row.get("reviewed_at")),
        "reviewed_by": _safe_str(row.get("reviewed_by")),
        "review_note": _safe_str(row.get("review_note")),
        "submission_count": submission_count,
        "pub_id_display": resolved_pub_id,
        "pub_href": pub_href,
        "pub_title": pub_title,
        "pub_doi": pub_doi,
        "pub_year": pub_year,
        "pub_missing": pub_missing,
        "repo_id_display": resolved_repo_id,
        "repo_href": repo_href,
        "repo_name": repo_name,
        "repo_graph_url": repo_graph_url,
        "repo_ambiguous": repo_ambiguous,
        "safe_repository_url": safe_repo_url,
        "safe_evidence_url": safe_ev_url,
        "implements_exists": implements_exists,
        "graph_up": graph_up,
        "prior": _safe_prior(prior),
        "can_recover": link_suggestions.is_recoverable(row),
        "recovery_reason": _safe_str(row.get("recovery_reason")),
    }


@router.get("/suggestions", response_class=HTMLResponse)
def queue(
    request: Request,
    user: CurrentUser,
    session: Session,
    db: Db,
    graph: MaybeGraph,
    tab: str = link_suggestions.PENDING,
    page: int = 1,
):
    """Display agent-submitted link evidence awaiting review or already moderated."""
    tab = _tab(tab, link_suggestions.PENDING)
    page = max(page, 1)
    try:
        counts = {name: link_suggestions.count(db, status=name) for name in TABS}
        total = counts[tab]
        rows = link_suggestions.list_suggestions(db, status=tab, limit=PAGE, skip=(page - 1) * PAGE)
        dedup_keys = [k for row in rows if isinstance(k := row.get("dedup_key"), str) and k]
        priors = (
            link_suggestions.prior_decisions(db, dedup_keys)
            if tab == link_suggestions.PENDING
            else {}
        )
    except PyMongoError as error:
        logger.warning("mongo failed while reading suggestions queue: %s", error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, MONGO_SILENT) from None
    back = f"/suggestions?tab={tab}&page={page}"

    active_graph = graph
    shown: list[dict[str, Any]] = []
    for row in rows:
        key = row.get("dedup_key")
        prior = priors.get(key) if isinstance(key, str) else None
        if prior and prior.get("_id") == row.get("_id"):
            prior = None
        if active_graph is not None:
            try:
                shown.append(_describe_row(row, prior=prior, graph=active_graph, back=back))
                continue
            except (ServiceUnavailable, AuthError, DriverError) as error:
                logger.warning("suggestions page shown without graph facts: %s", error)
                active_graph = None
        shown.append(_describe_row(row, prior=prior, graph=None, back=back))

    return templates.TemplateResponse(
        request,
        "suggestions.html",
        {
            "user": user,
            "csrf": session["csrf"],
            "tab": tab,
            "page": page,
            "pages": max((total + PAGE - 1) // PAGE, 1),
            "total": total,
            "counts": counts,
            "rows": shown,
            "graph_up": active_graph is not None,
        },
    )


class _UncertainGraphState(Exception):
    """Graph mutation outcome could not be verified or post-failure compensation failed."""


def _compensate_created_node(graph, db, label: str, node_id: str) -> bool:
    """Undo a newly created node and its CREATE override; False if cleanup could not finish."""
    ok = True
    try:
        delete_node(graph, label, node_id)
    except NotFound:
        pass
    except (MutationError, Neo4jError, DriverError) as error:
        logger.warning("could not delete compensated %s %s: %s", label, node_id, error)
        ok = False
    try:
        deactivate_override(db, label, node_id, only_op=CREATE)
    except PyMongoError as error:
        logger.warning("could not deactivate compensated CREATE override for %s %s: %s", label, node_id, error)
        ok = False
    return ok


def _compensate_claimed_override(db, label: str, node_id: str) -> bool:
    try:
        deactivate_override(db, label, node_id, only_op=CREATE)
        return True
    except PyMongoError as error:
        logger.warning("could not deactivate compensated %s claim %s: %s", label, node_id, error)
        return False


def _safe_release_claim(db, suggestion_id: str, apply_token: str) -> None:
    try:
        link_suggestions.release_claim(db, suggestion_id, apply_token)
    except PyMongoError as error:
        logger.warning("could not release apply claim for %s: %s", suggestion_id, error)


def _safe_mark_needs_recovery(db, suggestion_id: str, apply_token: str, reason: str = "") -> None:
    try:
        link_suggestions.mark_needs_recovery(db, suggestion_id, apply_token, reason=reason)
    except PyMongoError as error:
        logger.warning("could not mark recovery flag for %s: %s", suggestion_id, error)


def _apply_implements(
    graph,
    db,
    claimed: dict[str, Any],
    pub_id: str,
    *,
    actor: str,
    note: str,
) -> dict[str, Any]:
    repo_id, _repo_props, ambiguous = _resolve_repository(
        graph, claimed.get("repository_id"), claimed.get("repository_url")
    )
    if ambiguous:
        raise ValueError("По этому адресу найдено несколько репозиториев в графе; свяжите вручную.")
    created_repo = False
    if repo_id is None:
        normalized_url = normalize_repo_url_query(claimed.get("repository_url"))
        if normalized_url is None:
            raise ValueError("Не удалось создать репозиторий: некорректный адрес репозитория.")
        repo_id, repo_name = _new_repository_id(graph, normalized_url)
        props = {"url": normalized_url, **({"name": repo_name} if repo_name else {})}
        create_node(graph, "Repository", repo_id, props)
        _record(
            undo=lambda: delete_node(graph, "Repository", repo_id),
            write=lambda: record_override(
                db, "Repository", repo_id, CREATE, props, actor=actor, note=note
            ),
        )
        created_repo = True

    try:
        existed = _edge_exists(graph, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id) is not None
    except (MutationError, Neo4jError, DriverError):
        if created_repo and not _compensate_created_node(graph, db, "Repository", repo_id):
            raise _UncertainGraphState(
                "Не удалось откатить созданный репозиторий после сбоя проверки связи IMPLEMENTS."
            ) from None
        raise
    if not existed:
        created_edge = False
        try:
            create_relationship(graph, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id)
            created_edge = True
        except (MutationError, Neo4jError):
            if created_repo and not _compensate_created_node(graph, db, "Repository", repo_id):
                raise _UncertainGraphState(
                    "Не удалось откатить созданный репозиторий после сбоя создания связи IMPLEMENTS."
                ) from None
            raise
        except DriverError as error:
            try:
                probed = _edge_exists(graph, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id)
            except (MutationError, Neo4jError, DriverError):
                raise _UncertainGraphState(
                    "Связь с Neo4j оборвалась во время создания IMPLEMENTS; "
                    "проверьте и восстановите предложение после восстановления графа."
                ) from error
            if probed is not None:
                created_edge = True
            else:
                if created_repo and not _compensate_created_node(graph, db, "Repository", repo_id):
                    raise _UncertainGraphState(
                        "Связь IMPLEMENTS не создалась, но откат нового репозитория не завершился; "
                        "проверьте и восстановите предложение."
                    ) from error
                raise

        if created_edge:
            def _undo_implements() -> None:
                delete_relationship(graph, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id)
                if created_repo:
                    _compensate_created_node(graph, db, "Repository", repo_id)

            _record(
                undo=_undo_implements,
                write=lambda: record_relationship_override(
                    db, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id,
                    op=LINK, actor=actor, note=note,
                ),
            )
    else:
        _record(
            undo=lambda: None,
            write=lambda: record_relationship_override(
                db, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id,
                op=LINK, actor=actor, note=note,
            ),
        )
    return {
        "action": "implements",
        "publication_id": pub_id,
        "repository_id": repo_id,
        "created_repository": created_repo,
        "created_edge": not existed,
    }


def _apply_not_implements(
    graph,
    db,
    claimed: dict[str, Any],
    pub_id: str,
    *,
    actor: str,
    note: str,
) -> dict[str, Any]:
    repo_id, _repo_props, ambiguous = _resolve_repository(
        graph, claimed.get("repository_id"), claimed.get("repository_url")
    )
    if ambiguous:
        raise ValueError("По этому адресу найдено несколько репозиториев в графе; проверьте связь вручную.")
    if repo_id is None:
        return {"action": "noop_repo_absent", "publication_id": pub_id}
    snapshot = _edge_exists(graph, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id)
    if snapshot is not None:
        try:
            delete_relationship(graph, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id)
        except DriverError as error:
            try:
                still_there = _edge_exists(graph, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id)
            except (MutationError, Neo4jError, DriverError):
                raise _UncertainGraphState(
                    "Связь с Neo4j оборвалась при снятии IMPLEMENTS; "
                    "проверьте и восстановите предложение после восстановления графа."
                ) from error
            if still_there is not None:
                raise
        _record(
            undo=lambda: create_relationship(
                graph, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id, snapshot
            ),
            write=lambda: record_relationship_override(
                db, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id,
                op=DELETE, actor=actor, note=note, snapshot=snapshot,
            ),
        )
        return {
            "action": "removed_implements",
            "publication_id": pub_id,
            "repository_id": repo_id,
        }
    _record(
        undo=lambda: None,
        write=lambda: record_relationship_override(
            db, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id,
            op=DELETE, actor=actor, note=note, snapshot=None,
        ),
    )
    return {"action": "tombstoned_implements", "publication_id": pub_id, "repository_id": repo_id}


def _apply_mentions(
    graph,
    db,
    claimed: dict[str, Any],
    pub_id: str,
    *,
    actor: str,
    note: str,
) -> dict[str, Any]:
    repo_id, repo_props, ambiguous = _resolve_repository(
        graph, claimed.get("repository_id"), claimed.get("repository_url")
    )
    if ambiguous:
        raise ValueError("По этому адресу найдено несколько репозиториев в графе; свяжите вручную.")
    quote_text = claimed.get("quote")
    edge_props = (
        {
            "context": [quote_text],
            "page_number": [0],
            "pdf_id": [""],
            "context_source": [""],
            "classification_status": "classified",
        }
        if quote_text
        else {}
    )
    if repo_id is not None and repo_props is not None:
        repo_url = repo_props.get("url")
        if not isinstance(repo_url, str) or not repo_url:
            raise ValueError("У найденного репозитория не заполнено поле url для связи MENTIONS_LINK.")
        existed = _edge_exists(
            graph, "Publication", "MENTIONS_LINK", "Repository", pub_id, repo_url
        ) is not None
        if not existed:
            try:
                create_relationship(
                    graph, "Publication", "MENTIONS_LINK", "Repository", pub_id, repo_url, edge_props
                )
            except DriverError as error:
                try:
                    probed = _edge_exists(
                        graph, "Publication", "MENTIONS_LINK", "Repository", pub_id, repo_url
                    )
                except (MutationError, Neo4jError, DriverError):
                    raise _UncertainGraphState(
                        "Связь с Neo4j оборвалась при добавлении MENTIONS_LINK; "
                        "проверьте и восстановите предложение после восстановления графа."
                    ) from error
                if probed is None:
                    raise
            _record(
                undo=lambda: delete_relationship(
                    graph, "Publication", "MENTIONS_LINK", "Repository", pub_id, repo_url
                ),
                write=lambda: record_relationship_override(
                    db, "Publication", "MENTIONS_LINK", "Repository", pub_id, repo_url,
                    op=LINK, actor=actor, note=note,
                ),
            )
        else:
            _record(
                undo=lambda: None,
                write=lambda: record_relationship_override(
                    db, "Publication", "MENTIONS_LINK", "Repository", pub_id, repo_url,
                    op=LINK, actor=actor, note=note,
                ),
            )
        return {
            "action": "mentions_repository",
            "publication_id": pub_id,
            "repository_id": repo_id,
            "created_edge": not existed,
        }

    normalized_url = normalize_repo_url_query(claimed.get("repository_url"))
    if normalized_url is None:
        raise ValueError("Не удалось создать кандидата: некорректный адрес репозитория.")
    host = urlsplit(normalized_url).hostname or ""
    cand_props = {"url": normalized_url, "host": host}
    created_candidate = False
    claimed_candidate = False
    if graph.fetch_node_properties("LinkCandidate", normalized_url) is None:
        create_node(graph, "LinkCandidate", normalized_url, cand_props)
        _record(
            undo=lambda: delete_node(graph, "LinkCandidate", normalized_url),
            write=lambda: record_override(
                db, "LinkCandidate", normalized_url, CREATE, cand_props, actor=actor, note=note
            ),
        )
        created_candidate = True
    else:
        existing_override = db[OVERRIDES].find_one(
            {"_id": override_id("LinkCandidate", normalized_url), "active": True},
            {"op": 1},
        )
        if existing_override is None:
            _record(
                undo=lambda: None,
                write=lambda: record_override(
                    db, "LinkCandidate", normalized_url, CREATE, cand_props, actor=actor, note=note
                ),
            )
            claimed_candidate = True

    def _cleanup_candidate() -> bool:
        if created_candidate:
            return _compensate_created_node(graph, db, "LinkCandidate", normalized_url)
        if claimed_candidate:
            return _compensate_claimed_override(db, "LinkCandidate", normalized_url)
        return True

    try:
        existed = _edge_exists(
            graph, "Publication", "MENTIONS_LINK", "LinkCandidate", pub_id, normalized_url
        ) is not None
    except (MutationError, Neo4jError, DriverError):
        if not _cleanup_candidate():
            raise _UncertainGraphState(
                "Не удалось откатить LinkCandidate после сбоя проверки связи MENTIONS_LINK."
            ) from None
        raise
    if not existed:
        try:
            create_relationship(
                graph, "Publication", "MENTIONS_LINK", "LinkCandidate", pub_id, normalized_url, edge_props
            )
        except (MutationError, Neo4jError):
            if not _cleanup_candidate():
                raise _UncertainGraphState(
                    "Не удалось откатить LinkCandidate после сбоя создания связи MENTIONS_LINK."
                ) from None
            raise
        except DriverError as error:
            try:
                probed = _edge_exists(
                    graph, "Publication", "MENTIONS_LINK", "LinkCandidate", pub_id, normalized_url
                )
            except (MutationError, Neo4jError, DriverError):
                raise _UncertainGraphState(
                    "Связь с Neo4j оборвалась при добавлении MENTIONS_LINK для кандидата; "
                    "проверьте и восстановите предложение после восстановления графа."
                ) from error
            if probed is None:
                if not _cleanup_candidate():
                    raise _UncertainGraphState(
                        "Связь MENTIONS_LINK не создалась, но откат LinkCandidate не завершился; "
                        "проверьте и восстановите предложение."
                    ) from error
                raise

        def _undo_mentions_candidate() -> None:
            delete_relationship(graph, "Publication", "MENTIONS_LINK", "LinkCandidate", pub_id, normalized_url)
            _cleanup_candidate()

        _record(
            undo=_undo_mentions_candidate,
            write=lambda: record_relationship_override(
                db, "Publication", "MENTIONS_LINK", "LinkCandidate", pub_id, normalized_url,
                op=LINK, actor=actor, note=note,
            ),
        )
    else:
        _record(
            undo=lambda: None,
            write=lambda: record_relationship_override(
                db, "Publication", "MENTIONS_LINK", "LinkCandidate", pub_id, normalized_url,
                op=LINK, actor=actor, note=note,
            ),
        )
    return {
        "action": "mentions_candidate",
        "publication_id": pub_id,
        "candidate_id": normalized_url,
        "created_candidate": created_candidate,
        "created_edge": not existed,
    }


def _apply_full_text(
    graph,
    db,
    claimed: dict[str, Any],
    pub_id: str,
    pub_props: dict[str, Any],
    *,
    actor: str,
    note: str,
) -> dict[str, Any]:
    evidence_url = safe_evidence_url(claimed.get("evidence_url"))
    if evidence_url is None:
        raise ValueError("Некорректный URL полного текста.")
    previous = pub_props.get("pdf_urls")
    existing = [item for item in previous if isinstance(item, str)] if isinstance(previous, list) else []
    generation = override_generation(db, "Publication", pub_id)
    if evidence_url in existing:
        _record(
            undo=lambda: None,
            write=lambda: record_override(
                db,
                "Publication",
                pub_id,
                SET,
                actor=actor,
                note=note,
                expected_generation=generation,
                append_fields={"pdf_urls": [evidence_url]},
            ),
        )
        return {"action": "full_text_already_present", "publication_id": pub_id, "evidence_url": evidence_url}
    new_pdfs = [*existing, evidence_url]
    try:
        updated = update_node(
            graph,
            "Publication",
            pub_id,
            {"pdf_urls": new_pdfs},
            expected_updated_at=pub_props.get("updated_at"),
        )
    except DriverError as error:
        try:
            probed = read_node(graph, "Publication", pub_id)
        except (MutationError, Neo4jError, DriverError):
            raise _UncertainGraphState(
                "Связь с Neo4j оборвалась при добавлении ссылки PDF; "
                "проверьте и восстановите предложение после восстановления графа."
            ) from error
        probed_urls = probed.get("pdf_urls")
        if isinstance(probed_urls, list) and evidence_url in probed_urls:
            updated = probed
        else:
            raise
    _record(
        undo=lambda: update_node(
            graph,
            "Publication",
            pub_id,
            {"pdf_urls": previous},
            expected_updated_at=updated.get("updated_at"),
        ),
        write=lambda: record_override(
            db,
            "Publication",
            pub_id,
            SET,
            actor=actor,
            note=note,
            graph_updated_at=updated.get("updated_at"),
            expected_generation=generation,
            append_fields={"pdf_urls": [evidence_url]},
        ),
    )
    return {"action": "full_text_added", "publication_id": pub_id, "evidence_url": evidence_url}


def _matches_suggestion_note(override_doc: dict[str, Any] | None, suggestion_id: str) -> bool:
    note = (override_doc or {}).get("note")
    if not isinstance(note, str):
        return False
    prefix = f"предложение {suggestion_id}"
    return note == prefix or note.startswith(f"{prefix}:")


def _active_rel_override(
    db,
    src_label: str,
    rel_type: str,
    tgt_label: str,
    src_id: str,
    tgt_id: str,
    *,
    op: str,
) -> dict[str, Any] | None:
    return db[OVERRIDES].find_one(
        {
            "_id": relationship_override_id(src_label, rel_type, tgt_label, src_id, tgt_id),
            "active": True,
            "op": op,
        }
    )


def _inspect_applied_state(
    graph,
    db,
    suggestion: dict[str, Any],
) -> tuple[str, dict[str, Any] | str | None]:
    """Check Neo4j + `graph_overrides` to decide whether a stuck `applying` row succeeded."""
    sid = str(suggestion.get("_id") or "")
    pub_raw = suggestion.get("publication_id")
    if not isinstance(pub_raw, str) or not pub_raw:
        return "not_applied", None
    pub = _resolve_publication(graph, pub_raw)
    if pub is None:
        return "not_applied", None
    pub_id, pub_props = pub
    relation = suggestion.get("proposed_relation")

    if relation == link_suggestions.IMPLEMENTS:
        repo_id, _repo_props, ambiguous = _resolve_repository(
            graph, suggestion.get("repository_id"), suggestion.get("repository_url")
        )
        if ambiguous:
            return "partial", "По этому адресу найдено несколько репозиториев в графе; свяжите вручную."
        if repo_id is None:
            normalized_url = normalize_repo_url_query(suggestion.get("repository_url"))
            if normalized_url:
                cand_id, _ = _new_repository_id(graph, normalized_url)
                orphan_ov = db[OVERRIDES].find_one(
                    {"_id": override_id("Repository", cand_id), "active": True, "op": CREATE}
                )
                if _matches_suggestion_note(orphan_ov, sid):
                    return (
                        "partial",
                        "В ручных решениях осталось создание репозитория без узла в графе; проверьте и разберите вручную.",
                    )
            return "not_applied", None
        edge = _edge_exists(graph, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id)
        rel_ov = _active_rel_override(
            db, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id, op=LINK
        )
        node_ov = db[OVERRIDES].find_one(
            {"_id": override_id("Repository", repo_id), "active": True, "op": CREATE}
        )
        created_by_this = _matches_suggestion_note(node_ov, sid)
        if edge is not None and rel_ov is not None:
            return (
                "applied",
                {
                    "action": "implements",
                    "publication_id": pub_id,
                    "repository_id": repo_id,
                    "created_repository": created_by_this,
                    "created_edge": True,
                },
            )
        if edge is None and rel_ov is None and not created_by_this:
            return "not_applied", None
        return (
            "partial",
            "В графе или ручных решениях осталась незавершённая правка IMPLEMENTS; проверьте карточку.",
        )

    if relation == link_suggestions.NOT_IMPLEMENTS:
        repo_id, _repo_props, ambiguous = _resolve_repository(
            graph, suggestion.get("repository_id"), suggestion.get("repository_url")
        )
        if ambiguous:
            return "partial", "По этому адресу найдено несколько репозиториев в графе; проверьте связь вручную."
        if repo_id is None:
            return "not_applied", None
        edge = _edge_exists(graph, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id)
        del_ov = _active_rel_override(
            db, "Repository", "IMPLEMENTS", "Publication", repo_id, pub_id, op=DELETE
        )
        if edge is None and del_ov is not None:
            return (
                "applied",
                {"action": "removed_implements", "publication_id": pub_id, "repository_id": repo_id},
            )
        if edge is not None and del_ov is None:
            return "not_applied", None
        return (
            "partial",
            "Состояние снятия IMPLEMENTS между графом и ручными решениями расходится; проверьте карточку.",
        )

    if relation == link_suggestions.MENTIONS:
        repo_id, repo_props, ambiguous = _resolve_repository(
            graph, suggestion.get("repository_id"), suggestion.get("repository_url")
        )
        if ambiguous:
            return "partial", "По этому адресу найдено несколько репозиториев в графе; свяжите вручную."
        if repo_id is not None and repo_props is not None:
            repo_url = repo_props.get("url")
            if not isinstance(repo_url, str) or not repo_url:
                return "not_applied", None
            edge = _edge_exists(graph, "Publication", "MENTIONS_LINK", "Repository", pub_id, repo_url)
            rel_ov = _active_rel_override(
                db, "Publication", "MENTIONS_LINK", "Repository", pub_id, repo_url, op=LINK
            )
            if edge is not None and rel_ov is not None:
                return (
                    "applied",
                    {"action": "mentions_repository", "publication_id": pub_id, "repository_id": repo_id, "created_edge": True},
                )
            if edge is None and rel_ov is None:
                return "not_applied", None
            return (
                "partial",
                "В графе или ручных решениях осталась незавершённая правка MENTIONS_LINK; проверьте карточку.",
            )
        normalized_url = normalize_repo_url_query(suggestion.get("repository_url"))
        if normalized_url is None:
            return "not_applied", None
        cand = graph.fetch_node_properties("LinkCandidate", normalized_url)
        cand_ov = db[OVERRIDES].find_one(
            {"_id": override_id("LinkCandidate", normalized_url), "active": True, "op": CREATE}
        )
        edge = _edge_exists(
            graph, "Publication", "MENTIONS_LINK", "LinkCandidate", pub_id, normalized_url
        )
        rel_ov = _active_rel_override(
            db, "Publication", "MENTIONS_LINK", "LinkCandidate", pub_id, normalized_url, op=LINK
        )
        created_by_this = _matches_suggestion_note(cand_ov, sid)
        if cand is not None and edge is not None and rel_ov is not None and cand_ov is not None:
            return (
                "applied",
                {
                    "action": "mentions_candidate",
                    "publication_id": pub_id,
                    "candidate_id": normalized_url,
                    "created_candidate": created_by_this,
                    "created_edge": True,
                },
            )
        if edge is None and rel_ov is None and not created_by_this:
            return "not_applied", None
        return (
            "partial",
            "В графе или ручных решениях осталась незавершённая правка LinkCandidate; проверьте карточку.",
        )

    if relation == link_suggestions.FULL_TEXT:
        evidence_url = safe_evidence_url(suggestion.get("evidence_url"))
        if evidence_url is None:
            return "not_applied", None
        existing = pub_props.get("pdf_urls")
        pdf_list = [item for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
        node_ov = db[OVERRIDES].find_one({"_id": override_id("Publication", pub_id), "active": True})
        appended = ((node_ov or {}).get("append_fields") or {}).get("pdf_urls") or []
        has_ov = bool(node_ov and isinstance(appended, list) and evidence_url in appended)
        if evidence_url in pdf_list and has_ov:
            return (
                "applied",
                {"action": "full_text_added", "publication_id": pub_id, "evidence_url": evidence_url},
            )
        if not has_ov and not _matches_suggestion_note(node_ov, sid) and evidence_url not in pdf_list:
            return "not_applied", None
        return (
            "partial",
            "Состояние pdf_urls в графе и ручных решениях расходится; проверьте карточку публикации.",
        )

    return "not_applied", None


@router.post("/suggestions/{suggestion_id}/approve")
async def approve(
    suggestion_id: str,
    request: Request,
    user: Editor,
    db: Db,
    graph: Graph,
    _: CsrfChecked,
    __: StoresReady,
):
    """Apply an agent suggestion to Neo4j and persist a surviving override/claim."""
    form = await request.form()
    note = str(form.get("note", "")).strip()[:500]
    tab = _tab(form.get("tab"), link_suggestions.PENDING)
    try:
        current = link_suggestions.get(db, suggestion_id)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "такого предложения нет")
        claimed = link_suggestions.claim_for_apply(db, suggestion_id, user.actor)
    except PyMongoError as error:
        logger.warning("mongo failed before applying suggestion %s: %s", suggestion_id, error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, MONGO_SILENT) from None
    if claimed is None:
        latest = link_suggestions.get(db, suggestion_id) or current
        if latest.get("status") == link_suggestions.APPLYING:
            return _redirect(
                tab,
                problem="Предложение в статусе обработки; при зависании используйте проверку и восстановление.",
            )
        return _redirect(tab, problem="Это предложение уже рассмотрено или обрабатывается.")

    token = claimed["apply_token"]
    override_note = f"предложение {suggestion_id}: {note}" if note else f"предложение {suggestion_id}"
    try:
        pub = _resolve_publication(graph, claimed["publication_id"])
        if pub is None:
            _safe_release_claim(db, suggestion_id, token)
            return _redirect(tab, problem="Публикация не найдена в графе.")
        pub_id, pub_props = pub
        relation = claimed["proposed_relation"]
        if relation == link_suggestions.IMPLEMENTS:
            effect = _apply_implements(graph, db, claimed, pub_id, actor=user.actor, note=override_note)
        elif relation == link_suggestions.NOT_IMPLEMENTS:
            effect = _apply_not_implements(graph, db, claimed, pub_id, actor=user.actor, note=override_note)
        elif relation == link_suggestions.MENTIONS:
            effect = _apply_mentions(graph, db, claimed, pub_id, actor=user.actor, note=override_note)
        elif relation == link_suggestions.FULL_TEXT:
            effect = _apply_full_text(
                graph, db, claimed, pub_id, pub_props, actor=user.actor, note=override_note
            )
        else:
            raise ValueError(f"Неизвестный тип предложения: {relation}")
    except _UncertainGraphState as error:
        _safe_mark_needs_recovery(db, suggestion_id, token, reason=str(error))
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(error)) from None
    except ValueError as error:
        _safe_release_claim(db, suggestion_id, token)
        return _redirect(tab, problem=str(error))
    except VersionConflict:
        _safe_release_claim(db, suggestion_id, token)
        return _redirect(tab, problem="Публикация обновилась параллельно. Повторите одобрение.")
    except MutationError as error:
        _safe_release_claim(db, suggestion_id, token)
        return _redirect(tab, problem=f"Не удалось применить изменение в графе: {error}")
    except (ServiceUnavailable, AuthError, DriverError) as error:
        _safe_release_claim(db, suggestion_id, token)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Граф Neo4j недоступен; изменение не применено ({error}).",
        ) from None
    except Neo4jError as error:
        _safe_release_claim(db, suggestion_id, token)
        return _redirect(
            tab,
            problem=f"Ошибка Neo4j при применении: {error.code or type(error).__name__}",
        )
    except PyMongoError as error:
        logger.warning("mongo failed while preparing suggestion %s: %s", suggestion_id, error)
        _safe_release_claim(db, suggestion_id, token)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, MONGO_SILENT) from None
    except HTTPException as exc:
        detail = str(exc.detail)
        if "не откатывали" in detail or "не откатилась" in detail:
            _safe_mark_needs_recovery(db, suggestion_id, token, reason=detail)
        else:
            _safe_release_claim(db, suggestion_id, token)
        raise

    try:
        completed = link_suggestions.complete_approval(
            db,
            suggestion_id,
            token,
            actor=user.actor,
            note=note,
            applied_effect=effect,
        )
    except PyMongoError as error:
        logger.exception("mongo failed completing approval of %s after graph commit: %s", suggestion_id, error)
        msg = (
            "Изменение записано в граф и ручные решения, но статус предложения в MongoDB не обновился; "
            "восстановите статус на странице предложений после восстановления соединения."
        )
        _safe_mark_needs_recovery(db, suggestion_id, token, reason=msg)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, msg) from None
    if completed is None:
        logger.warning("%s applied suggestion %s, but claim token expired before completion", user.actor, suggestion_id)
        return _redirect(
            tab,
            problem="Изменение применено в граф, но срок блокировки предложения истёк; обновите страницу и проверьте статус.",
        )
    logger.info("%s approved suggestion %s (%s)", user.actor, suggestion_id, effect.get("action"))
    return _redirect(tab, done="approved")


@router.post("/suggestions/{suggestion_id}/recover")
async def recover(
    suggestion_id: str,
    request: Request,
    user: Editor,
    db: Db,
    graph: Graph,
    _: CsrfChecked,
    __: StoresReady,
):
    """Reconcile a stuck `applying` suggestion against Neo4j and `graph_overrides`."""
    form = await request.form()
    note = str(form.get("note", "")).strip()[:500]
    tab = _tab(form.get("tab"), link_suggestions.PENDING)
    try:
        if link_suggestions.get(db, suggestion_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "такого предложения нет")
        claimed = link_suggestions.claim_for_recovery(db, suggestion_id, user.actor)
    except PyMongoError as error:
        logger.warning("mongo failed before recovering suggestion %s: %s", suggestion_id, error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, MONGO_SILENT) from None
    if claimed is None:
        return _redirect(
            tab,
            problem="Восстановление сейчас недоступно: предложение уже завершено или ещё активно обрабатывается.",
        )

    token = claimed["apply_token"]
    try:
        state, detail = _inspect_applied_state(graph, db, claimed)
    except (ServiceUnavailable, AuthError, DriverError, Neo4jError) as error:
        _safe_mark_needs_recovery(db, suggestion_id, token)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Граф Neo4j недоступен; проверить состояние предложения не удалось ({error}).",
        ) from None
    except PyMongoError as error:
        logger.warning("mongo failed while inspecting suggestion %s: %s", suggestion_id, error)
        _safe_mark_needs_recovery(db, suggestion_id, token)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, MONGO_SILENT) from None

    try:
        if state == "applied":
            completed = link_suggestions.complete_approval(
                db,
                suggestion_id,
                token,
                actor=user.actor,
                note=note or "восстановлено после сбоя",
                applied_effect=detail if isinstance(detail, dict) else {},
            )
            if completed is None:
                return _redirect(tab, problem="Предложение обновилось параллельно; обновите страницу.")
            logger.info("%s recovered suggestion %s as approved", user.actor, suggestion_id)
            return _redirect(tab, done="recovered_approved")
        if state == "not_applied":
            if not link_suggestions.release_claim(db, suggestion_id, token):
                return _redirect(tab, problem="Предложение обновилось параллельно; обновите страницу.")
            logger.info("%s recovered suggestion %s back to pending", user.actor, suggestion_id)
            return _redirect(tab, done="recovered_pending")
        diag = str(detail or "Состояние в графе частично применено; проверьте и разберите вручную.")
        link_suggestions.mark_needs_recovery(db, suggestion_id, token, reason=diag)
        return _redirect(tab, problem=diag)
    except PyMongoError as error:
        logger.warning("mongo failed while finalizing recovery for %s: %s", suggestion_id, error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, MONGO_SILENT) from None


@router.post("/suggestions/{suggestion_id}/reject")
async def reject(
    suggestion_id: str,
    request: Request,
    user: Editor,
    db: Db,
    _: CsrfChecked,
    __: StoresReady,
):
    """Reject a pending suggestion without touching Neo4j or graph_overrides."""
    form = await request.form()
    note = str(form.get("note", "")).strip()[:500]
    tab = _tab(form.get("tab"), link_suggestions.PENDING)
    try:
        current = link_suggestions.get(db, suggestion_id)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "такого предложения нет")
        updated = link_suggestions.reject(db, suggestion_id, actor=user.actor, note=note)
    except PyMongoError as error:
        logger.warning("mongo failed while rejecting suggestion %s: %s", suggestion_id, error)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, MONGO_SILENT) from None
    if updated is None:
        latest = link_suggestions.get(db, suggestion_id) or current
        if latest.get("status") == link_suggestions.APPLYING:
            return _redirect(
                tab,
                problem="Предложение находится в статусе обработки; отклонить напрямую нельзя — сначала проверьте и восстановите статус.",
            )
        return _redirect(tab, problem="Это предложение уже рассмотрено или обрабатывается.")
    logger.info("%s rejected suggestion %s", user.actor, suggestion_id)
    return _redirect(tab, done="rejected")
