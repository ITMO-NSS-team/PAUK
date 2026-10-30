"""Apply account matches without losing the independent person data underneath."""

from pauk.models.person import GitHubMatchResult, GitHubMatchState, Person


def retract_matches(person: Person) -> list[GitHubMatchResult]:
    """Remove recorded additions, preserving later source changes.

    Returns:
        The matches that were applied, for retaining or replacing them.
    """
    state = person.github_match
    if state is None:
        return []
    if state.github is not None and person.github == state.github:
        person.github = None
    if state.email is not None and person.email == state.email:
        independently_known = any(address.strip().casefold() == state.email.casefold()
                                  for address in person.emails)
        if not independently_known:
            person.email = None
    person.contributed_to = [contribution for contribution in person.contributed_to
                             if contribution not in state.contributed_to]
    person.github_match = None
    return state.matches


def apply_matches(person: Person, matches: list[GitHubMatchResult]) -> None:
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
    state = GitHubMatchState(matches=[combined[login] for login in sorted(combined)])
    for match in state.matches:
        if not person.github:
            person.github = state.github = match.login
        if not person.email and match.email:
            person.email = state.email = match.email
        for contribution in match.contributed_to:
            if contribution not in person.contributed_to:
                person.contributed_to.append(contribution.model_copy(deep=True))
                state.contributed_to.append(contribution.model_copy(deep=True))
    person.github_match = state
