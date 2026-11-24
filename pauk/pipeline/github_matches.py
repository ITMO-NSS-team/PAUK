"""Apply account matches without losing the independent person data underneath."""

from collections.abc import Iterable

from pauk.models.person import Contribution, GitHubMatchField, GitHubMatchResult, GitHubMatchState, Person


def retract_matches(person: Person) -> list[GitHubMatchResult]:
    """Remove recorded additions, preserving later source changes.

    Returns:
        The matches that were applied, for retaining or replacing them.
    """
    state = person.github_match
    if state is None:
        return []
    cleared = set(state.cleared_fields)
    withdrawn_logins = set(state.withdrawn_logins) | {match.login for match in state.matches}
    if state.github is not None and person.github == state.github:
        person.github = None
        cleared.add("github")
    if state.email is not None and person.email == state.email:
        independently_known = any(address.strip().casefold() == state.email.casefold()
                                  for address in person.emails)
        if not independently_known:
            person.email = None
            cleared.add("email")
    cleared_contributions = list(state.cleared_contributions)
    for contribution in state.contributed_to:
        if contribution in person.contributed_to and contribution not in cleared_contributions:
            cleared_contributions.append(contribution.model_copy(deep=True))
    person.contributed_to = [contribution for contribution in person.contributed_to
                             if contribution not in state.contributed_to]
    person.github_match = (GitHubMatchState(cleared_fields=sorted(cleared),
                                          cleared_contributions=cleared_contributions,
                                          withdrawn_logins=sorted(withdrawn_logins))
                           if cleared or cleared_contributions or withdrawn_logins else None)
    return state.matches


def apply_matches(person: Person, matches: list[GitHubMatchResult], *,
                  cleared_fields: Iterable[GitHubMatchField] = (),
                  cleared_contributions: Iterable[Contribution] = (),
                  withdrawn_logins: Iterable[str] = ()) -> None:
    """Overlay matches and record only values absent from independent sources."""
    combined: dict[str, GitHubMatchResult] = {}
    for match in matches:
        if match.login not in combined:
            combined[match.login] = match.model_copy(deep=True)
            continue
        known = combined[match.login]
        known.email = known.email or match.email
        for contribution in match.contributed_to:
            # A refreshed role must not be overwritten by its earlier value.
            if not any(previous.repository_id == contribution.repository_id for previous in known.contributed_to):
                known.contributed_to.append(contribution.model_copy(deep=True))
    cleared = set(person.github_match.cleared_fields if person.github_match else ()) | set(cleared_fields)
    withdrawn = list(person.github_match.cleared_contributions if person.github_match else ())
    for contribution in cleared_contributions:
        if contribution not in withdrawn:
            withdrawn.append(contribution.model_copy(deep=True))
    state = GitHubMatchState(matches=[combined[login] for login in sorted(combined)],
                            cleared_fields=sorted(cleared))
    for match in state.matches:
        if not person.github:
            person.github = state.github = match.login
        if not person.email and match.email:
            person.email = state.email = match.email
        for contribution in match.contributed_to:
            if contribution not in person.contributed_to:
                person.contributed_to.append(contribution.model_copy(deep=True))
                state.contributed_to.append(contribution.model_copy(deep=True))
    state.cleared_fields = [field for field in state.cleared_fields if getattr(person, field) is None]
    state.cleared_contributions = [contribution for contribution in withdrawn
                                   if not any(current.repository_id == contribution.repository_id
                                              for current in person.contributed_to)]
    retired = set(person.github_match.withdrawn_logins if person.github_match else ()) | set(withdrawn_logins)
    state.withdrawn_logins = sorted(retired - combined.keys())
    person.github_match = state


def withdraw_matches(person: Person, logins: set[str], *, clear_login: bool = True) -> bool:
    """Withdraw named accounts without reevaluating the person's other matches."""
    tracked = person.github_match.matches if person.github_match else ()
    if not (clear_login and person.github in logins) and not any(match.login in logins for match in tracked):
        return False
    previous = retract_matches(person)
    legacy_refusal = clear_login and person.github in logins
    legacy_login = person.github if legacy_refusal else None
    if legacy_refusal:
        person.github = None
    apply_matches(person, [match for match in previous if match.login not in logins],
                  cleared_fields=("github",) if legacy_refusal else (),
                  withdrawn_logins=(legacy_login,) if legacy_login else ())
    return True
