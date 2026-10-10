# `emails` stage

**What this covers:** where author addresses come from, and why an address from
a page is not assigned to just anyone.

**Files involved:** `pauk/pipeline/stages/emails.py`.

Reads the full text of publications downloaded by `code_links`, and the
personal pages authors listed in ORCID. Fills `Person.email` and
`Person.emails`. Runs before `github_match`: a found address is the strongest
signal for recognizing an account.

## Address from a paper

In a paper's text an address sits next to the author list, and there is almost
never just one. Three forms are parsed: plain, a `mailto:` link, and the group
form `{ivanov, petrov}@itmo.ru` that publishers use to avoid printing a shared
domain three times.

## Whose address it is

An address is assigned to an author only if the local part (before the @)
contains their surname. Without this check the first address in the text would
go to every author of the paper. If the surname is found for two people, the
paper has namesakes and the address names neither unambiguously; such an
address is not taken at all.

The surname here is the last word of the Latin spelling of the name, long
enough not to be an initial (`author_surnames`). The matcher uses the same
function, so both stages read a name the same way.

## Address from an author's page

For ITMO people who still have no address, the stage downloads the page the
author listed in ORCID (`Person.homepage`). It takes `mailto` links and text
with obfuscation removed: `name [at] itmo [dot] ru`, `&#64;`, spaces around
the at sign, which is how addresses are hidden from spam bots on lab pages.

The surname check is the same, for the same reason: a lab page lists the whole
group, and without it an employee would get the head's address.

Download errors are silent: the page is a supplementary source, and a dead link
must not fail the run.

## Which address to show

`Person.emails` holds all known addresses, and the matcher recognizes an
account by them. `Person.email` is a single one, for the card: an institutional
address beats a personal one, and on a tie the shorter wins (`pick_email`).
Addresses like `...@users.noreply.github.com` are not real addresses and are
dropped.
