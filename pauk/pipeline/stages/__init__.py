from .code_links import CodeLinksStage
from .dedup import DedupStage
from .departments import DepartmentsStage
from .emails import EmailsStage
from .github_match import GitHubMatchStage
from .link_relevance import LinkRelevanceStage
from .pdf import PdfStage
from .persons import PersonsStage
from .repositories import RepositoriesStage
from .russian_names import RussianNamesStage

# Dedup runs after the fetching stages: it folds duplicate publications,
# repositories and persons using what they fetched (ORCIDs and name variants,
# GitHub repo ids) and rewrites every prepared row that referenced a
# merged-away id. Russian names come last so only canonical persons are named
# — and so a name is resolved against every spelling the merge collected,
# which is more than the survivor carried on its own. Dedup does not wait for
# that: it reads the staff catalog directly (dedup.staff_identities), since a
# record in it is an identity, and identity is exactly what dedup needs.
# link_relevance runs right after code_links, which is what produces the
# unclassified (is_relevant=None) links it judges. emails reads the full
# text code_links downloaded, and runs before github_match so the addresses
# it finds can identify an account. github_match needs the accounts
# repositories harvests and the authorships dedup has already folded.
ALL_STAGES = (
    PdfStage, PersonsStage, DepartmentsStage, CodeLinksStage, LinkRelevanceStage,
    EmailsStage, RepositoriesStage, DedupStage, GitHubMatchStage, RussianNamesStage,
)